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
from .storage import dataset_lock_path, dataset_path, exclusive_file_lock, read_dataset, write_dataset


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
    max_new_orders: int = 5
    price_offset_bps: float = 2.0
    cancel_stale_minutes: int = 5
    submit_orders: bool = False
    confirmed: bool = False
    allow_market_exit: bool = True
    cancel_entries_before_exit: bool = True
    exit_slippage_tolerance_bps: float = 50.0
    pause_new_entries: bool = False
    entry_leverage: float = 1.0
    require_contiguous_twap: bool = True
    order_link_prefix: str = ""


@dataclass(frozen=True, slots=True)
class DemoCancelAllConfig:
    symbols: tuple[str, ...] = ()
    account_type: str = "UNIFIED"
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class DemoFlattenConfig:
    confirmed: bool = False
    account_type: str = "UNIFIED"


_SUBMITTED_STATUSES = {"accepted", "placed", "submitted", "cancel_requested", "exit_submitted", "submit_unknown"}
_BLOCKING_RECONCILED_STATUSES = {
    "accepted",
    "open_order_seen",
    "position_detected",
    "filled",
    "partial",
    "exit_submitted",
    "submit_unknown",
    "filled_pending_execs",
}
_TERMINAL_RECONCILED_STATUSES = {"cancelled", "missed_entry", "submit_not_found", "partial_cancelled"}
_ACTIVE_ORDER_STATUSES = {"new", "created", "untriggered", "triggered", "partiallyfilled"}
_FILLED_ORDER_STATUSES = {"filled"}
_CANCELLED_ORDER_STATUSES = {"cancelled", "rejected", "deactivated", "partiallyfilledcanceled"}
_SUBMIT_UNKNOWN_GRACE_MS = 60_000


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
    _validate_demo_sync_config(sync_config)
    if sync_config.submit_orders and not sync_config.confirmed:
        raise RuntimeError("Refusing demo sync order submission without --i-understand-demo-sync")

    now_dt = _as_utc(now or datetime.now(tz=UTC))
    now_ms = _floor_minute_ms(int(now_dt.timestamp() * 1000))
    trades = read_dataset(data_root, "forward_paper_trades")
    paper_slices = read_dataset(data_root, "forward_paper_slices")
    market = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
    executor = _demo_executor(config, sync_config, execution_client, api_key, api_secret)
    instruments = {str(row.get("symbol", "")).upper(): row for row in market.get_instruments_info()}

    with exclusive_file_lock(dataset_lock_path(data_root, "demo_execution_orders")):
        existing = read_dataset(data_root, "demo_execution_orders")
        reconciled = reconcile_demo_orders(existing, now=now_dt, execution_client=executor) if executor is not None else existing
        reconciled = cancel_stale_demo_orders(reconciled, now=now_dt, sync_config=sync_config, execution_client=executor)
        if executor is not None and sync_config.cancel_entries_before_exit:
            exit_symbols = _closed_trade_exit_symbols(trades, reconciled)
            reconciled = _cancel_open_entry_orders_for_symbols(
                reconciled,
                symbols=exit_symbols,
                now=now_dt,
                execution_client=executor,
            )
        new_orders = build_demo_sync_orders(
            trades,
            paper_slices=paper_slices,
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
                "paper_slices": paper_slices.height,
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


def _validate_demo_sync_config(sync_config: DemoSyncConfig) -> None:
    if sync_config.max_new_orders < 0:
        raise ValueError("max_new_orders cannot be negative")
    if sync_config.entry_leverage < 0.0:
        raise ValueError("entry_leverage must be non-negative")
    if sync_config.exit_slippage_tolerance_bps < 0.0:
        raise ValueError("exit_slippage_tolerance_bps must be non-negative")


def run_bybit_demo_cancel_all(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    cancel_config: DemoCancelAllConfig | None = None,
    now: datetime | None = None,
    execution_client: Any | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> dict[str, Any]:
    cancel = cancel_config or DemoCancelAllConfig()
    if not cancel.confirmed:
        raise RuntimeError("Refusing demo cancel-all without --i-understand-demo-cancel-all")
    now_dt = _as_utc(now or datetime.now(tz=UTC))
    executor = _demo_private_executor(config, execution_client, api_key, api_secret)
    symbols = tuple(symbol.strip().upper() for symbol in cancel.symbols if symbol.strip())
    targets = symbols or ("*",)
    results: list[dict[str, Any]] = []
    for target in targets:
        try:
            result = executor.cancel_all_orders(
                symbol=None if target == "*" else target,
                settle_coin=config.exchange.settle_coin,
            )
            results.append({"target": target, "status": "cancel_requested", "result": result, "error": ""})
        except Exception as exc:  # noqa: BLE001 - keep the emergency report complete
            results.append({"target": target, "status": "cancel_failed", "result": {}, "error": str(exc)})
    payload = {
        "now": now_dt.isoformat(),
        "command": "bybit-demo-cancel-all",
        "config": asdict(cancel),
        "rows": {"targets": len(targets), "cancel_requested": _count_payload_status(results, "cancel_requested")},
        "results": results,
    }
    _write_demo_emergency_report(data_root, "bybit_demo_cancel_all_report", payload, format_demo_cancel_all_report(payload))
    return payload


def run_bybit_demo_flatten(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    flatten_config: DemoFlattenConfig,
    now: datetime | None = None,
    execution_client: Any | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> dict[str, Any]:
    if not flatten_config.confirmed:
        raise RuntimeError("Refusing demo flatten without --i-understand-demo-flatten")
    now_dt = _as_utc(now or datetime.now(tz=UTC))
    now_ms = int(now_dt.timestamp() * 1000)
    executor = _demo_private_executor(config, execution_client, api_key, api_secret)
    positions = executor.get_positions(settle_coin=config.exchange.settle_coin)
    results: list[dict[str, Any]] = []
    for position in positions:
        symbol = str(position.get("symbol") or "").upper()
        side = str(position.get("side") or "")
        size = Decimal(str(position.get("size") or "0"))
        if not symbol or size <= 0:
            continue
        exit_side = "Buy" if side.lower() == "sell" else "Sell"
        request = {
            "symbol": symbol,
            "side": exit_side,
            "orderType": "Market",
            "qty": _decimal_text(size),
            "timeInForce": "IOC",
            "positionIdx": int(position.get("positionIdx") or position.get("position_idx") or 0),
            "reduceOnly": True,
            "orderLinkId": _emergency_order_link_id("flat", symbol, now_ms),
        }
        try:
            result = executor.place_order(**request)
            results.append(
                {
                    "symbol": symbol,
                    "position_side": side,
                    "position_size": float(size),
                    "status": "flatten_submitted",
                    "request": request,
                    "result": result,
                    "error": "",
                }
            )
        except Exception as exc:  # noqa: BLE001 - try the remaining positions
            results.append(
                {
                    "symbol": symbol,
                    "position_side": side,
                    "position_size": float(size),
                    "status": "flatten_failed",
                    "request": request,
                    "result": {},
                    "error": str(exc),
                }
            )
    payload = {
        "now": now_dt.isoformat(),
        "command": "bybit-demo-flatten",
        "config": asdict(flatten_config),
        "rows": {
            "positions_seen": len(positions),
            "positions_with_size": len(results),
            "flatten_submitted": _count_payload_status(results, "flatten_submitted"),
            "flatten_failed": _count_payload_status(results, "flatten_failed"),
        },
        "results": results,
    }
    _write_demo_emergency_report(data_root, "bybit_demo_flatten_report", payload, format_demo_flatten_report(payload))
    return payload


def submit_demo_exit_for_trade(
    data_root: str | Path,
    trade: dict[str, Any],
    *,
    sync_config: DemoSyncConfig,
    now: datetime,
    execution_client: Any,
    whole_position: bool = False,
    instrument: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit one idempotent reduce-only exit through the shared demo ledger."""
    now_dt = _as_utc(now)
    now_ms = int(now_dt.timestamp() * 1000)
    with exclusive_file_lock(dataset_lock_path(data_root, "demo_execution_orders")):
        existing = read_dataset(data_root, "demo_execution_orders")
        reconciled = reconcile_demo_orders(existing, now=now_dt, execution_client=execution_client)
        trade_id = str(trade.get("trade_id") or "")
        if _has_blocking_action(reconciled, trade_id, "exit"):
            return {
                "status": "blocked_duplicate_exit",
                "row": {},
                "ledger_orders": reconciled.height,
                "order_link_id": _sync_order_link_id(trade_id, "exit", prefix=sync_config.order_link_prefix),
            }
        if sync_config.cancel_entries_before_exit:
            reconciled = _cancel_open_entry_orders_for_symbols(
                reconciled,
                symbols={str(trade.get("symbol") or "").upper()},
                now=now_dt,
                execution_client=execution_client,
            )
        row = _exit_order_row(
            trade,
            existing_orders=reconciled,
            sync_config=sync_config,
            now_ms=now_ms,
            execution_client=execution_client,
            whole_position=whole_position,
            instrument=instrument,
        )
        if row.get("status") == "pending_submit":
            row = _submit_pending_order_row(
                row,
                execution_client=execution_client,
                sync_config=sync_config,
                leverage_guarded_symbols=set(),
            )
        new_orders = pl.DataFrame([row], infer_schema_length=None)
        ledger = _merge_order_frames(reconciled, new_orders)
        payload = {
            "now": now_dt.isoformat(),
            "rows": {
                "paper_trades": 0,
                "paper_slices": 0,
                "existing_orders": existing.height,
                "new_orders": 1,
                "ledger_orders": ledger.height,
            },
            "summary": summarize_demo_sync_orders(ledger),
            "config": asdict(sync_config),
            "new_orders": [row],
        }
        _write_demo_sync_outputs(data_root, ledger, payload=payload)
        return {
            "status": str(row.get("status") or ""),
            "reconciled_status": str(row.get("reconciled_status") or ""),
            "row": row,
            "ledger_orders": ledger.height,
            "order_link_id": str(row.get("order_link_id") or ""),
        }


def build_demo_sync_orders(
    trades: pl.DataFrame,
    *,
    paper_slices: pl.DataFrame | None = None,
    existing_orders: pl.DataFrame,
    instruments: dict[str, dict[str, Any]],
    sync_config: DemoSyncConfig,
    now_ms: int,
    market_client: Any,
    execution_client: Any | None,
) -> pl.DataFrame:
    if trades.is_empty() or sync_config.max_new_orders <= 0:
        return pl.DataFrame()
    slices_by_trade = _paper_slices_by_trade(paper_slices if paper_slices is not None else pl.DataFrame())
    candidate_rows: list[dict[str, Any]] = []
    sorted_trades = trades.sort(["entry_ts_ms", "symbol"]) if "entry_ts_ms" in trades.columns else trades
    trade_rows = sorted_trades.to_dicts()

    for trade in trade_rows:
        trade_id = str(trade.get("trade_id") or "")
        symbol = str(trade.get("symbol") or "").upper()
        if not trade_id or not symbol:
            continue
        if (
            trade.get("status") == "closed"
            and _has_blocking_action(existing_orders, trade_id, "entry")
            and not _has_blocking_action(existing_orders, trade_id, "exit")
        ):
            try:
                row = _exit_order_row(
                    trade,
                    existing_orders=existing_orders,
                    sync_config=sync_config,
                    now_ms=now_ms,
                    execution_client=execution_client,
                    whole_position=True,
                    instrument=instruments.get(symbol, {}),
                    market_client=market_client,
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol should not block the batch
                row = _skip_row(
                    trade,
                    "exit",
                    now_ms,
                    f"build_failed: {exc}",
                    order_link_prefix=sync_config.order_link_prefix,
                )
            candidate_rows.append(row)

    for trade in trade_rows:
        trade_id = str(trade.get("trade_id") or "")
        symbol = str(trade.get("symbol") or "").upper()
        if not trade_id or not symbol:
            continue
        if trade.get("status") != "open":
            continue
        if sync_config.pause_new_entries:
            candidate_rows.append(
                _skip_row(
                    trade,
                    "entry",
                    now_ms,
                    "new_entries_paused",
                    order_link_prefix=sync_config.order_link_prefix,
                )
            )
            continue
        if symbol not in instruments:
            candidate_rows.append(
                _skip_row(
                    trade,
                    "entry",
                    now_ms,
                    "instrument_missing",
                    order_link_prefix=sync_config.order_link_prefix,
                )
            )
            continue
        trade_slices = slices_by_trade.get(trade_id, []) if int(_float(trade.get("entry_twap_minutes"))) > 0 else []
        if trade_slices:
            due_slices = _due_demo_entry_slices(trade_slices, now_ms=now_ms)
            for slice_row in due_slices:
                slice_ts_ms = int(slice_row.get("scheduled_ts_ms") or 0)
                if _has_blocking_action(existing_orders, trade_id, "entry", slice_ts_ms=slice_ts_ms) or (
                    not sync_config.submit_orders
                    and _has_dry_run_action(existing_orders, trade_id, "entry", slice_ts_ms=slice_ts_ms)
                ):
                    continue
                if sync_config.require_contiguous_twap and _has_missing_prior_twap_attempt(
                    existing_orders,
                    trade_id=trade_id,
                    trade_slices=trade_slices,
                    slice_ts_ms=slice_ts_ms,
                    dry_run_counts=not sync_config.submit_orders,
                ):
                    candidate_rows.append(
                        _skip_row(
                            trade,
                            "entry",
                            now_ms,
                            "missed_prior_twap_slice",
                            order_link_prefix=sync_config.order_link_prefix,
                            paper_slice=slice_row,
                        )
                    )
                    continue
                try:
                    row = _entry_order_row(
                        trade,
                        instrument=instruments[symbol],
                        sync_config=sync_config,
                        now_ms=now_ms,
                        market_client=market_client,
                        execution_client=execution_client,
                        paper_slice=slice_row,
                    )
                except Exception as exc:  # noqa: BLE001 - one bad symbol should not block the batch
                    row = _skip_row(
                        trade,
                        "entry",
                        now_ms,
                        f"build_failed: {exc}",
                        order_link_prefix=sync_config.order_link_prefix,
                        paper_slice=slice_row,
                    )
                candidate_rows.append(row)
            continue
        if _has_blocking_action(existing_orders, trade_id, "entry"):
            continue
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
            row = _skip_row(
                trade,
                "entry",
                now_ms,
                f"build_failed: {exc}",
                order_link_prefix=sync_config.order_link_prefix,
            )
        candidate_rows.append(row)

    capped_rows = _cap_candidate_order_rows(candidate_rows, sync_config=sync_config, now_ms=now_ms)
    leverage_guarded_symbols = _entry_leverage_guarded_symbols(existing_orders, date=_dt_from_ms(now_ms).date().isoformat())
    rows = [
        _submit_pending_order_row(
            row,
            execution_client=execution_client,
            sync_config=sync_config,
            leverage_guarded_symbols=leverage_guarded_symbols,
        )
        if row.get("status") == "pending_submit"
        else row
        for row in capped_rows
    ]
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def reconcile_demo_orders(
    orders: pl.DataFrame,
    *,
    now: datetime,
    execution_client: Any,
) -> pl.DataFrame:
    if orders.is_empty():
        return orders
    order_rows = orders.to_dicts()
    rows_to_reconcile = [row for row in order_rows if _row_needs_reconcile(row)]
    if not rows_to_reconcile:
        return orders
    symbols = sorted({str(row.get("symbol") or "").upper() for row in rows_to_reconcile if str(row.get("symbol") or "")})
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
    for row in order_rows:
        if not _row_needs_reconcile(row):
            rows.append(row)
            continue
        updated = dict(row)
        symbol = str(row.get("symbol") or "").upper()
        order_link_id = str(row.get("order_link_id") or "")
        order_id = str(row.get("order_id") or "")
        open_orders = open_by_symbol.get(symbol, [])
        positions = positions_by_symbol.get(symbol, [])
        order_history = _private_history_rows(
            execution_client,
            "get_order_history",
            symbol=symbol,
            order_link_id=order_link_id,
        )
        trade_history = _private_history_rows(
            execution_client,
            "get_trade_history",
            symbol=symbol,
            order_link_id=order_link_id,
        )
        open_error = next((item["__error__"] for item in open_orders if "__error__" in item), None)
        position_error = next((item["__error__"] for item in positions if "__error__" in item), None)
        order_history_error = next((item["__error__"] for item in order_history if "__error__" in item), None)
        trade_history_error = next((item["__error__"] for item in trade_history if "__error__" in item), None)
        open_match = next((item for item in open_orders if _matches_order(item, order_link_id, order_id)), None)
        order_matches = [item for item in order_history if _matches_order(item, order_link_id, order_id)]
        trade_matches = [item for item in trade_history if _matches_order(item, order_link_id, order_id)]
        position = _symbol_position(positions, symbol)
        history_status = _history_order_status(order_matches)
        filled_qty, filled_value = _filled_totals(trade_matches)
        updated["reconcile_ts_ms"] = int(now.timestamp() * 1000)
        updated["reconcile_time"] = now.isoformat()
        updated["open_order_seen"] = bool(open_match)
        updated["open_order_status"] = str(open_match.get("orderStatus") or "") if open_match else ""
        updated["order_history_seen"] = bool(order_matches)
        updated["order_history_status"] = history_status
        updated["trade_history_seen"] = bool(trade_matches)
        updated["trade_count"] = len(trade_matches)
        updated["filled_qty"] = filled_qty
        updated["filled_value"] = filled_value
        updated["position_side"] = position["side"]
        updated["position_size"] = position["size"]
        updated["position_value"] = position["value"]
        updated["reconcile_error"] = open_error or position_error or order_history_error or trade_history_error
        updated["reconciled_status"] = _reconciled_status(
            updated,
            open_match=open_match,
            position=position,
            history_status=history_status,
            filled_qty=filled_qty,
        )
        rows.append(updated)
    return pl.DataFrame(rows, infer_schema_length=None)


def _row_needs_reconcile(row: dict[str, Any]) -> bool:
    if _row_is_terminal_reconciled(row):
        return False
    status = str(row.get("status") or "").strip().lower()
    if status in {"", "dry_run", "skipped", "place_failed", "cancel_failed"}:
        return False
    return bool(str(row.get("order_link_id") or "") or str(row.get("order_id") or ""))


def _row_is_terminal_reconciled(row: dict[str, Any]) -> bool:
    reconciled_status = str(row.get("reconciled_status") or "").strip().lower()
    if reconciled_status in _TERMINAL_RECONCILED_STATUSES:
        return True
    return (
        reconciled_status == "filled"
        and _float(row.get("filled_qty")) > 0.0
        and _float(row.get("filled_value")) > 0.0
    )


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
            not _row_is_terminal_reconciled(updated)
            and status in {"accepted", "placed"}
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


def _closed_trade_exit_symbols(trades: pl.DataFrame, orders: pl.DataFrame) -> set[str]:
    if trades.is_empty():
        return set()
    symbols: set[str] = set()
    for trade in trades.to_dicts():
        trade_id = str(trade.get("trade_id") or "")
        symbol = str(trade.get("symbol") or "").upper()
        if (
            trade_id
            and symbol
            and str(trade.get("status") or "") == "closed"
            and _has_blocking_action(orders, trade_id, "entry")
            and not _has_blocking_action(orders, trade_id, "exit")
        ):
            symbols.add(symbol)
    return symbols


def _cancel_open_entry_orders_for_symbols(
    orders: pl.DataFrame,
    *,
    symbols: set[str],
    now: datetime,
    execution_client: Any,
) -> pl.DataFrame:
    symbols = {symbol.upper() for symbol in symbols if symbol}
    if orders.is_empty() or not symbols:
        return orders
    now_ms = int(now.timestamp() * 1000)
    rows = []
    for row in orders.to_dicts():
        updated = dict(row)
        symbol = str(updated.get("symbol") or "").upper()
        if (
            symbol in symbols
            and str(updated.get("action") or "") == "entry"
            and not bool(updated.get("reduce_only"))
            and not _row_is_terminal_reconciled(updated)
            and str(updated.get("status") or "").strip().lower() != "cancel_requested"
            and bool(updated.get("open_order_seen"))
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
            "accepted": 0,
            "dry_run": 0,
            "skipped": 0,
            "cancel_requested": 0,
            "submit_unknown": 0,
            "open_order_seen": 0,
            "estimated_notional": 0.0,
        }
    return {
        "orders": orders.height,
        "placed": _count_status(orders, "placed") + _count_status(orders, "accepted"),
        "accepted": _count_status(orders, "accepted"),
        "dry_run": _count_status(orders, "dry_run"),
        "skipped": _count_status(orders, "skipped"),
        "cancel_requested": _count_status(orders, "cancel_requested"),
        "place_failed": _count_status(orders, "place_failed"),
        "submit_unknown": _count_status(orders, "submit_unknown"),
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
        f"| Paper slices | {payload.get('rows', {}).get('paper_slices', 0)} |",
        f"| New orders | {payload.get('rows', {}).get('new_orders', 0)} |",
        f"| Ledger orders | {summary.get('orders', 0)} |",
        f"| Placed | {summary.get('placed', 0)} |",
        f"| Accepted | {summary.get('accepted', 0)} |",
        f"| Dry run | {summary.get('dry_run', 0)} |",
        f"| Skipped | {summary.get('skipped', 0)} |",
        f"| Cancel requested | {summary.get('cancel_requested', 0)} |",
        f"| Submit unknown | {summary.get('submit_unknown', 0)} |",
        f"| Open order seen | {summary.get('open_order_seen', 0)} |",
        f"| Estimated notional | {summary.get('estimated_notional', 0.0):.2f} |",
        "",
        "## Recent Orders",
        "",
        "| Time | Action | Slice | Symbol | Side | Status | Qty | Price | Notional | Paper Trade | Reconciled | Error |",
        "|---|---|---:|---|---|---|---:|---:|---:|---|---|---|",
    ]
    if not orders.is_empty():
        for row in orders.sort("created_ts_ms", descending=True).head(50).to_dicts():
            lines.append(
                f"| {row.get('created_time', '')} | {row.get('action', '')} | {row.get('slice_time', '')} | "
                f"{row.get('symbol', '')} | "
                f"{row.get('side', '')} | {row.get('status', '')} | {row.get('qty', '')} | "
                f"{row.get('price', '')} | {float(row.get('estimated_notional') or 0.0):.2f} | "
                f"{row.get('paper_trade_id', '')} | {row.get('reconciled_status', '')} | "
                f"{str(row.get('error') or row.get('reconcile_error') or '')[:120]} |"
            )
    lines.append("")
    return "\n".join(lines)


def format_demo_cancel_all_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Bybit Demo Cancel All",
        "",
        f"Now: {payload.get('now')}",
        "",
        "| Target | Status | Error |",
        "|---|---|---|",
    ]
    for row in payload.get("results", []):
        lines.append(f"| {row.get('target')} | {row.get('status')} | {str(row.get('error') or '')[:160]} |")
    lines.append("")
    return "\n".join(lines)


def format_demo_flatten_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Bybit Demo Flatten",
        "",
        f"Now: {payload.get('now')}",
        "",
        "| Symbol | Position Side | Position Size | Status | Order Link ID | Error |",
        "|---|---|---:|---|---|---|",
    ]
    for row in payload.get("results", []):
        request = row.get("request") or {}
        lines.append(
            f"| {row.get('symbol')} | {row.get('position_side')} | {float(row.get('position_size') or 0.0):.8f} | "
            f"{row.get('status')} | {request.get('orderLinkId', '')} | {str(row.get('error') or '')[:160]} |"
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


def _paper_slices_by_trade(paper_slices: pl.DataFrame) -> dict[str, list[dict[str, Any]]]:
    if paper_slices.is_empty() or "trade_id" not in paper_slices.columns:
        return {}
    rows = paper_slices.sort(["trade_id", "slice_index"]).to_dicts()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("trade_id") or ""), []).append(row)
    return grouped


def _due_demo_entry_slices(rows: list[dict[str, Any]], *, now_ms: int) -> list[dict[str, Any]]:
    due = []
    for row in rows:
        scheduled_ts_ms = int(row.get("scheduled_ts_ms") or 0)
        if scheduled_ts_ms != now_ms:
            continue
        status = str(row.get("status") or "")
        if status in {"filled", "missed_no_bar", "pending"}:
            due.append(row)
    return due


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
    paper_slice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = str(trade["symbol"]).upper()
    side = "Sell" if str(trade.get("side", "short")).lower() == "short" else "Buy"
    planned_notional = _paper_slice_notional(trade, paper_slice)
    if planned_notional <= 0.0:
        return _skip_row(
            trade,
            "entry",
            now_ms,
            "non_positive_notional",
            order_link_prefix=sync_config.order_link_prefix,
            paper_slice=paper_slice,
        )
    quote = _top_of_book(market_client.get_orderbook(symbol, limit=1))
    order = _build_limit_order(
        symbol=symbol,
        side=side,
        notional=planned_notional,
        max_notional=planned_notional,
        price_offset_bps=sync_config.price_offset_bps,
        quote=quote,
        instrument=instrument,
        order_link_id=_entry_order_link_id(trade, "entry", sync_config=sync_config, paper_slice=paper_slice),
        reduce_only=False,
    )
    return _candidate_order_row(
        trade,
        action="entry",
        order=order,
        status_if_dry="dry_run",
        now_ms=now_ms,
        execution_client=execution_client,
        paper_slice=paper_slice,
    )


def _exit_order_row(
    trade: dict[str, Any],
    *,
    existing_orders: pl.DataFrame,
    sync_config: DemoSyncConfig,
    now_ms: int,
    execution_client: Any | None,
    whole_position: bool = True,
    instrument: dict[str, Any] | None = None,
    market_client: Any | None = None,
) -> dict[str, Any]:
    symbol = str(trade["symbol"]).upper()
    position = _symbol_position(execution_client.get_positions(symbol=symbol) if execution_client is not None else [], symbol)
    if position["size"] <= 0.0:
        return _skip_row(
            trade,
            "exit",
            now_ms,
            "no_demo_position_detected",
            order_link_prefix=sync_config.order_link_prefix,
        )
    if not sync_config.allow_market_exit:
        return _skip_row(
            trade,
            "exit",
            now_ms,
            "market_exit_disabled",
            order_link_prefix=sync_config.order_link_prefix,
        )
    entry_qty = _entry_qty_for_trade(existing_orders, str(trade["trade_id"]))
    qty = Decimal(str(position["size"])) if whole_position else (
        min(Decimal(str(position["size"])), entry_qty) if entry_qty > 0 else Decimal(str(position["size"]))
    )
    if qty <= 0:
        return _skip_row(
            trade,
            "exit",
            now_ms,
            "non_positive_exit_qty",
            order_link_prefix=sync_config.order_link_prefix,
        )
    side = "Buy" if str(position["side"]).lower() == "sell" else "Sell"
    order_link_id = _exit_order_link_id(
        str(trade["trade_id"]),
        existing_orders,
        prefix=sync_config.order_link_prefix,
    )
    reference_price = _exit_reference_price(trade)
    if market_client is not None and not _has_explicit_exit_execution_reference(trade):
        try:
            quote = _top_of_book(market_client.get_orderbook(symbol, limit=1))
            live_reference = quote["ask"] if side == "Buy" else quote["bid"]
            if live_reference > 0.0:
                reference_price = live_reference
        except Exception:  # noqa: BLE001 - use the paper/model reference if the live book is unavailable
            pass
    request = {
        "symbol": symbol,
        "side": side,
        "qty": _decimal_text(qty),
        "timeInForce": "IOC",
        "positionIdx": position["idx"],
        "reduceOnly": True,
        "orderLinkId": order_link_id,
    }
    if reference_price > 0.0 and sync_config.exit_slippage_tolerance_bps > 0.0:
        request["orderType"] = "Limit"
        request["price"] = _decimal_text(
            _bounded_exit_limit_price(
                side=side,
                reference_price=reference_price,
                slippage_bps=sync_config.exit_slippage_tolerance_bps,
                instrument=instrument or {},
            )
        )
    else:
        request["orderType"] = "Market"
        if sync_config.exit_slippage_tolerance_bps > 0.0:
            request["slippageToleranceType"] = "Percent"
            request["slippageTolerance"] = _slippage_tolerance_percent_text(sync_config.exit_slippage_tolerance_bps)
    order = {
        "request": request,
        "estimated_notional": float(position["value"]),
        "max_order_notional": float(position["value"]),
        "price_offset_bps": sync_config.exit_slippage_tolerance_bps if reference_price > 0.0 else 0.0,
    }
    return _candidate_order_row(
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


def _candidate_order_row(
    trade: dict[str, Any],
    *,
    action: str,
    order: dict[str, Any],
    status_if_dry: str,
    now_ms: int,
    execution_client: Any | None,
    paper_slice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = dict(order["request"])
    status = "pending_submit" if execution_client is not None else status_if_dry
    scheduled_ts_ms = int(paper_slice.get("scheduled_ts_ms") or 0) if paper_slice else 0
    slice_index = int(paper_slice.get("slice_index") or 0) if paper_slice else 0
    return {
        "order_link_id": request["orderLinkId"],
        "order_id": "",
        "paper_trade_id": str(trade.get("trade_id") or ""),
        "basket_id": str(trade.get("basket_id") or ""),
        "date": str(trade.get("date") or _dt_from_ms(now_ms).date().isoformat()),
        "action": action,
        "slice_index": slice_index,
        "slice_ts_ms": scheduled_ts_ms or None,
        "slice_time": _dt_from_ms(scheduled_ts_ms).isoformat() if scheduled_ts_ms else "",
        "paper_slice_status": str(paper_slice.get("status") or "") if paper_slice else "",
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
        "place_result": "",
        "leverage_result": "",
        "cancel_result": "",
        "cancel_ts_ms": None,
        "cancel_time": None,
        "open_order_seen": False,
        "open_order_status": "",
        "order_history_seen": False,
        "order_history_status": "",
        "trade_history_seen": False,
        "trade_count": 0,
        "filled_qty": 0.0,
        "filled_value": 0.0,
        "position_side": "",
        "position_size": 0.0,
        "position_value": 0.0,
        "reconciled_status": "",
        "reconcile_ts_ms": None,
        "reconcile_time": None,
        "reconcile_error": None,
        "error": None,
    }


def _cap_candidate_order_rows(
    rows: list[dict[str, Any]],
    *,
    sync_config: DemoSyncConfig,
    now_ms: int,
) -> list[dict[str, Any]]:
    capped: list[dict[str, Any]] = []
    new_orders = 0
    for row in rows:
        if not _row_has_order_request(row):
            capped.append(row)
            continue
        if new_orders >= sync_config.max_new_orders:
            capped.append(_skip_candidate_row(row, now_ms=now_ms, reason="max_new_orders_exceeded"))
            continue
        capped.append(row)
        new_orders += 1
    return capped


def _row_has_order_request(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "") in {"dry_run", "pending_submit"} and bool(row.get("request"))


def _skip_candidate_row(row: dict[str, Any], *, now_ms: int, reason: str) -> dict[str, Any]:
    updated = dict(row)
    updated["status"] = "skipped"
    updated["estimated_notional"] = 0.0
    updated["request"] = ""
    updated["reconciled_status"] = ""
    updated["error"] = reason
    updated["created_ts_ms"] = now_ms
    updated["created_time"] = _dt_from_ms(now_ms).isoformat()
    return updated


def _submit_pending_order_row(
    row: dict[str, Any],
    *,
    execution_client: Any | None,
    sync_config: DemoSyncConfig,
    leverage_guarded_symbols: set[str],
) -> dict[str, Any]:
    if execution_client is None:
        return row
    updated = dict(row)
    try:
        request = json.loads(str(updated.get("request") or "{}"))
        if not request:
            raise ValueError("missing order request")
        if _needs_entry_leverage_guard(updated, request, sync_config=sync_config):
            symbol = str(request.get("symbol") or "")
            if symbol and symbol not in leverage_guarded_symbols:
                leverage_result = _set_entry_leverage(
                    execution_client,
                    symbol=symbol,
                    leverage=sync_config.entry_leverage,
                )
                updated["leverage_result"] = json.dumps(leverage_result, sort_keys=True) if leverage_result else ""
                leverage_guarded_symbols.add(symbol)
    except Exception as exc:  # noqa: BLE001
        updated["status"] = "place_failed"
        updated["place_result"] = ""
        updated["reconciled_status"] = ""
        updated["error"] = str(exc)
        return updated
    try:
        place_result = execution_client.place_order(**request)
        updated["status"] = "accepted"
        updated["order_id"] = str(place_result.get("orderId") or "")
        updated["place_result"] = json.dumps(place_result, sort_keys=True)
        updated["reconciled_status"] = "exit_submitted" if str(updated.get("action")) == "exit" else "accepted"
        updated["error"] = None
    except Exception as exc:  # noqa: BLE001
        updated["status"] = "submit_unknown"
        updated["place_result"] = ""
        updated["reconciled_status"] = "submit_unknown"
        updated["error"] = str(exc)
    return updated


def _needs_entry_leverage_guard(row: dict[str, Any], request: dict[str, Any], *, sync_config: DemoSyncConfig) -> bool:
    return (
        sync_config.entry_leverage > 0.0
        and str(row.get("action") or "") == "entry"
        and not bool(request.get("reduceOnly"))
    )


def _set_entry_leverage(execution_client: Any, *, symbol: str, leverage: float) -> dict[str, Any] | None:
    setter = getattr(execution_client, "set_leverage", None)
    if not callable(setter):
        return None
    return setter(symbol=symbol, buy_leverage=leverage, sell_leverage=leverage)


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


def _write_demo_emergency_report(data_root: str | Path, stem: str, payload: dict[str, Any], markdown: str) -> None:
    output_dir = Path(data_root) / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{stem}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / f"{stem}.md").write_text(markdown, encoding="utf-8")


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


def _demo_private_executor(
    config: ResearchConfig,
    execution_client: Any | None,
    api_key: str | None,
    api_secret: str | None,
) -> Any:
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


def _skip_row(
    trade: dict[str, Any],
    action: str,
    now_ms: int,
    reason: str,
    *,
    order_link_prefix: str = "",
    paper_slice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    order_link_id = _entry_order_link_id(
        trade,
        action,
        sync_config=DemoSyncConfig(order_link_prefix=order_link_prefix),
        paper_slice=paper_slice,
    )
    scheduled_ts_ms = int(paper_slice.get("scheduled_ts_ms") or 0) if paper_slice else 0
    slice_index = int(paper_slice.get("slice_index") or 0) if paper_slice else 0
    return {
        "order_link_id": order_link_id,
        "order_id": "",
        "paper_trade_id": str(trade.get("trade_id") or ""),
        "basket_id": str(trade.get("basket_id") or ""),
        "date": str(trade.get("date") or _dt_from_ms(now_ms).date().isoformat()),
        "action": action,
        "slice_index": slice_index,
        "slice_ts_ms": scheduled_ts_ms or None,
        "slice_time": _dt_from_ms(scheduled_ts_ms).isoformat() if scheduled_ts_ms else "",
        "paper_slice_status": str(paper_slice.get("status") or "") if paper_slice else "",
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
        "leverage_result": "",
        "cancel_result": "",
        "cancel_ts_ms": None,
        "cancel_time": None,
        "open_order_seen": False,
        "open_order_status": "",
        "order_history_seen": False,
        "order_history_status": "",
        "trade_history_seen": False,
        "trade_count": 0,
        "filled_qty": 0.0,
        "filled_value": 0.0,
        "position_side": "",
        "position_size": 0.0,
        "position_value": 0.0,
        "reconciled_status": "",
        "reconcile_ts_ms": None,
        "reconcile_time": None,
        "reconcile_error": None,
        "error": reason,
    }


def _has_blocking_action(
    orders: pl.DataFrame,
    trade_id: str,
    action: str,
    *,
    slice_ts_ms: int | None = None,
) -> bool:
    if orders.is_empty() or {"paper_trade_id", "action"}.difference(set(orders.columns)):
        return False
    frame = orders.filter((pl.col("paper_trade_id") == trade_id) & (pl.col("action") == action))
    if slice_ts_ms is not None and "slice_ts_ms" in frame.columns:
        frame = frame.filter(pl.col("slice_ts_ms").fill_null(0).cast(pl.Int64) == int(slice_ts_ms))
    rows = frame.to_dicts()
    return any(_row_blocks_duplicate(row) for row in rows)


def _has_dry_run_action(
    orders: pl.DataFrame,
    trade_id: str,
    action: str,
    *,
    slice_ts_ms: int | None = None,
) -> bool:
    if orders.is_empty() or {"paper_trade_id", "action", "status"}.difference(set(orders.columns)):
        return False
    frame = orders.filter((pl.col("paper_trade_id") == trade_id) & (pl.col("action") == action))
    if slice_ts_ms is not None and "slice_ts_ms" in frame.columns:
        frame = frame.filter(pl.col("slice_ts_ms").fill_null(0).cast(pl.Int64) == int(slice_ts_ms))
    rows = frame.to_dicts()
    return any(str(row.get("status") or "").strip().lower() == "dry_run" for row in rows)


def _has_missing_prior_twap_attempt(
    orders: pl.DataFrame,
    *,
    trade_id: str,
    trade_slices: list[dict[str, Any]],
    slice_ts_ms: int,
    dry_run_counts: bool,
) -> bool:
    prior_slice_times = sorted(
        {
            int(row.get("scheduled_ts_ms") or 0)
            for row in trade_slices
            if int(row.get("scheduled_ts_ms") or 0) and int(row.get("scheduled_ts_ms") or 0) < slice_ts_ms
        }
    )
    if not prior_slice_times:
        return False
    return any(
        not _has_entry_slice_attempt(
            orders,
            trade_id=trade_id,
            slice_ts_ms=ts_ms,
            dry_run_counts=dry_run_counts,
        )
        for ts_ms in prior_slice_times
    )


def _has_entry_slice_attempt(
    orders: pl.DataFrame,
    *,
    trade_id: str,
    slice_ts_ms: int,
    dry_run_counts: bool,
) -> bool:
    if orders.is_empty() or {"paper_trade_id", "action", "slice_ts_ms", "status"}.difference(set(orders.columns)):
        return False
    frame = orders.filter(
        (pl.col("paper_trade_id") == trade_id)
        & (pl.col("action") == "entry")
        & (pl.col("slice_ts_ms").fill_null(0).cast(pl.Int64) == int(slice_ts_ms))
    )
    if frame.is_empty():
        return False
    rows = frame.to_dicts()
    return any(_row_counts_as_twap_attempt(row, dry_run_counts=dry_run_counts) for row in rows)


def _row_counts_as_twap_attempt(row: dict[str, Any], *, dry_run_counts: bool) -> bool:
    if dry_run_counts and str(row.get("status") or "").strip().lower() == "dry_run":
        return True
    status = str(row.get("status") or "").strip().lower()
    if status == "place_failed" and bool(str(row.get("request") or "")):
        return True
    if status == "skipped" and str(row.get("error") or "") == "missed_prior_twap_slice":
        return True
    if str(row.get("reconciled_status") or "").strip().lower() in _TERMINAL_RECONCILED_STATUSES:
        return True
    return _row_blocks_duplicate(row)


def _entry_leverage_guarded_symbols(orders: pl.DataFrame, *, date: str) -> set[str]:
    if orders.is_empty() or {"action", "symbol", "status", "date"}.difference(set(orders.columns)):
        return set()
    frame = orders.filter((pl.col("action") == "entry") & (pl.col("date") == date))
    if frame.is_empty():
        return set()
    symbols: set[str] = set()
    for row in frame.to_dicts():
        status = str(row.get("status") or "").strip().lower()
        reconciled_status = str(row.get("reconciled_status") or "").strip().lower()
        if (
            status in _SUBMITTED_STATUSES
            or reconciled_status in _BLOCKING_RECONCILED_STATUSES
            or reconciled_status in _TERMINAL_RECONCILED_STATUSES
            or reconciled_status == "filled"
        ):
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                symbols.add(symbol)
    return symbols


def _row_blocks_duplicate(row: dict[str, Any]) -> bool:
    reconciled_status = str(row.get("reconciled_status") or "").strip().lower()
    if reconciled_status in _TERMINAL_RECONCILED_STATUSES:
        return False
    if reconciled_status in _BLOCKING_RECONCILED_STATUSES:
        return True
    return str(row.get("status") or "").strip().lower() in _SUBMITTED_STATUSES


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
    blocking_rows = [row for row in rows if _row_blocks_duplicate(row)]
    return blocking_rows[-1] if blocking_rows else None


def _entry_qty_for_trade(orders: pl.DataFrame, trade_id: str) -> Decimal:
    if orders.is_empty() or {"paper_trade_id", "action"}.difference(set(orders.columns)):
        return Decimal("0")
    rows = orders.filter((pl.col("paper_trade_id") == trade_id) & (pl.col("action") == "entry")).to_dicts()
    total = Decimal("0")
    for row in rows:
        if _row_blocks_duplicate(row):
            total += Decimal(str(row.get("qty") or "0"))
    return total


def _merge_order_frames(left: pl.DataFrame, right: pl.DataFrame) -> pl.DataFrame:
    frames = [frame for frame in (left, right) if not frame.is_empty()]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed").unique(subset=["order_link_id"], keep="last").sort(
        ["created_ts_ms", "symbol"]
    )


def _private_history_rows(
    execution_client: Any,
    method_name: str,
    *,
    symbol: str,
    order_link_id: str,
) -> list[dict[str, Any]]:
    method = getattr(execution_client, method_name, None)
    if not callable(method) or not order_link_id:
        return []
    try:
        rows = method(symbol=symbol or None, order_link_id=order_link_id)
        if isinstance(rows, list):
            return rows
        if isinstance(rows, dict):
            return rows.get("result", {}).get("list", [])
        return []
    except Exception as exc:  # noqa: BLE001
        return [{"__error__": str(exc)}]


def _matches_order(row: dict[str, Any], order_link_id: str, order_id: str) -> bool:
    if "__error__" in row:
        return False
    row_link_id = str(row.get("orderLinkId") or row.get("order_link_id") or "")
    row_order_id = str(row.get("orderId") or row.get("order_id") or "")
    return bool((order_link_id and row_link_id == order_link_id) or (order_id and row_order_id == order_id))


def _history_order_status(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        status = str(row.get("orderStatus") or row.get("order_status") or "")
        if status:
            return status
    return ""


def _filled_totals(rows: list[dict[str, Any]]) -> tuple[float, float]:
    qty = 0.0
    value = 0.0
    for row in rows:
        if "__error__" in row:
            continue
        qty += _float(row.get("execQty", row.get("qty", row.get("exec_qty"))))
        value += _float(row.get("execValue", row.get("exec_value", row.get("tradeValue"))))
    return qty, value


def _reconciled_status(
    row: dict[str, Any],
    *,
    open_match: dict[str, Any] | None,
    position: dict[str, Any],
    history_status: str,
    filled_qty: float,
) -> str:
    action = str(row.get("action") or "")
    status = str(row.get("status") or "").strip().lower()
    prior_reconciled_status = str(row.get("reconciled_status") or "").strip().lower()
    open_status = _normalized_order_status(open_match.get("orderStatus")) if open_match else ""
    normalized_history_status = _normalized_order_status(history_status)
    order_qty = _float(row.get("qty"))
    has_position = bool(position["size"])
    has_reconcile_error = bool(row.get("reconcile_error"))

    has_fill_value = _float(row.get("filled_value")) > 0.0
    if normalized_history_status in _FILLED_ORDER_STATUSES or (order_qty > 0.0 and filled_qty >= order_qty):
        return "filled" if filled_qty > 0.0 and has_fill_value else "filled_pending_execs"
    if action == "exit" and filled_qty > 0.0 and not has_position:
        return "filled" if has_fill_value else "filled_pending_execs"
    if action == "exit" and filled_qty > 0.0 and normalized_history_status in _CANCELLED_ORDER_STATUSES:
        return "partial_cancelled"
    if filled_qty > 0.0 or open_status == "partiallyfilled" or normalized_history_status == "partiallyfilled":
        return "partial"
    if normalized_history_status in _CANCELLED_ORDER_STATUSES or open_status in _CANCELLED_ORDER_STATUSES:
        return "missed_entry" if action == "entry" and not has_position else "cancelled"
    if status == "cancel_requested":
        return "cancel_requested"
    if open_match and (not open_status or open_status in _ACTIVE_ORDER_STATUSES):
        return "open_order_seen"
    if action == "entry" and has_position:
        return "position_detected"
    if prior_reconciled_status in _TERMINAL_RECONCILED_STATUSES:
        return prior_reconciled_status
    if has_reconcile_error and status not in _SUBMITTED_STATUSES:
        return "reconcile_error"
    if status == "submit_unknown":
        created_ts_ms = int(_float(row.get("created_ts_ms")))
        reconcile_ts_ms = int(_float(row.get("reconcile_ts_ms")))
        if has_reconcile_error or reconcile_ts_ms <= 0 or created_ts_ms <= 0:
            return "submit_unknown"
        if reconcile_ts_ms - created_ts_ms < _SUBMIT_UNKNOWN_GRACE_MS:
            return "submit_unknown"
        return "submit_not_found"
    if action == "exit" and status in _SUBMITTED_STATUSES:
        return "exit_submitted"
    if status in _SUBMITTED_STATUSES:
        return "accepted"
    return str(row.get("reconciled_status") or "")


def _normalized_order_status(value: Any) -> str:
    return str(value or "").replace("_", "").replace("-", "").strip().lower()


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


def _exit_reference_price(trade: dict[str, Any]) -> float:
    for key in ("execution_exit_price", "trigger_price", "fast_trigger_price", "model_exit_price", "exit_price", "paper_exit_price"):
        value = _float(trade.get(key))
        if value > 0.0:
            return value
    return 0.0


def _has_explicit_exit_execution_reference(trade: dict[str, Any]) -> bool:
    return any(_float(trade.get(key)) > 0.0 for key in ("execution_exit_price", "trigger_price", "fast_trigger_price"))


def _paper_slice_notional(
    trade: dict[str, Any],
    paper_slice: dict[str, Any] | None,
) -> float:
    total = _paper_notional(trade)
    if paper_slice is None:
        return total
    expected = int(_float(trade.get("entry_twap_minutes")) or _float(paper_slice.get("entry_twap_minutes")) or 0)
    if expected <= 0:
        return total
    target_total = _float(trade.get("target_notional"))
    if target_total > 0.0:
        total = target_total
    return total / expected if total > 0.0 else 0.0


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
            "idx": int(_float(row.get("positionIdx", row.get("position_idx", 0)))),
        }
    return {"side": "", "size": 0.0, "value": 0.0, "idx": 0}


def _sync_order_link_id(trade_id: str, action: str, *, prefix: str = "") -> str:
    clean_prefix = "".join(char for char in prefix.lower() if char.isalnum())[:8]
    action_key = "".join(char for char in action.lower() if char.isalnum())[:1] or "x"
    digest = blake2b(f"{clean_prefix}:{trade_id}:{action}".encode("utf-8"), digest_size=8).hexdigest()
    return f"agc{clean_prefix}{action_key}{digest}"[:36]


def _exit_order_link_id(trade_id: str, existing_orders: pl.DataFrame, *, prefix: str = "") -> str:
    attempt = _prior_exit_attempt_count(existing_orders, trade_id) + 1
    return _sync_order_link_id(f"{trade_id}:attempt:{attempt}", "exit", prefix=prefix)


def _prior_exit_attempt_count(existing_orders: pl.DataFrame, trade_id: str) -> int:
    if existing_orders.is_empty() or {"paper_trade_id", "action"}.difference(set(existing_orders.columns)):
        return 0
    return existing_orders.filter((pl.col("paper_trade_id") == trade_id) & (pl.col("action") == "exit")).height


def _entry_order_link_id(
    trade: dict[str, Any],
    action: str,
    *,
    sync_config: DemoSyncConfig,
    paper_slice: dict[str, Any] | None = None,
) -> str:
    if paper_slice is None:
        return _sync_order_link_id(str(trade.get("trade_id") or "missing"), action, prefix=sync_config.order_link_prefix)
    prefix = "".join(char for char in sync_config.order_link_prefix.lower() if char.isalnum())[:5] or "slv"
    action_key = "".join(char for char in action.lower() if char.isalnum())[:1] or "x"
    symbol = "".join(char for char in str(trade.get("symbol") or "").lower() if char.isalnum())[:8] or "sym"
    date_token = "".join(char for char in str(trade.get("date") or "").replace("-", "") if char.isdigit())[2:8] or "000000"
    slice_ts_ms = int(paper_slice.get("scheduled_ts_ms") or 0)
    slice_time = _dt_from_ms(slice_ts_ms).strftime("%H%M") if slice_ts_ms else "0000"
    digest = blake2b(
        f"{prefix}:{trade.get('trade_id')}:{action}:{symbol}:{date_token}:{slice_time}:{slice_ts_ms}".encode("utf-8"),
        digest_size=4,
    ).hexdigest()
    return f"agc{prefix}{action_key}{symbol}{date_token}{slice_time}{digest}"[:36]


def _count_status(orders: pl.DataFrame, status: str) -> int:
    return 0 if orders.is_empty() or "status" not in orders.columns else int((orders["status"] == status).sum())


def _count_payload_status(rows: list[dict[str, Any]], status: str) -> int:
    return sum(1 for row in rows if row.get("status") == status)


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


def _bounded_exit_limit_price(
    *,
    side: str,
    reference_price: float,
    slippage_bps: float,
    instrument: dict[str, Any],
) -> Decimal:
    tick_size = _decimal_filter(instrument, "priceFilter", "tickSize", "0.0001")
    reference = Decimal(str(reference_price))
    offset = Decimal(str(slippage_bps)) / Decimal("10000")
    if reference <= 0:
        raise ValueError(f"non-positive bounded exit reference price: {reference}")
    if side == "Buy":
        price = reference * (Decimal("1") + offset)
        return _round_to_step(price, tick_size, ROUND_CEILING)
    if side == "Sell":
        price = reference * (Decimal("1") - offset)
        rounded = _round_to_step(price, tick_size, ROUND_FLOOR)
        if rounded <= 0:
            raise ValueError(f"computed non-positive bounded exit limit price: {rounded}")
        return rounded
    raise ValueError(f"unknown exit side: {side}")


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


def _slippage_tolerance_percent_text(slippage_bps: float) -> str:
    percent = Decimal(str(slippage_bps)) / Decimal("100")
    percent = max(Decimal("0.01"), min(percent, Decimal("10")))
    return _decimal_text(percent.quantize(Decimal("0.01")))


def _order_link_id(symbol: str, timestamp_ms: int) -> str:
    suffix = "".join(char for char in symbol.upper() if char.isalnum())[:10]
    return f"agcdemo{timestamp_ms % 100000000000:011d}{suffix}"[:36]


def _emergency_order_link_id(prefix: str, symbol: str, timestamp_ms: int) -> str:
    clean_prefix = "".join(char for char in prefix.lower() if char.isalnum())[:8]
    clean_symbol = "".join(char for char in symbol.upper() if char.isalnum())[:12].lower()
    digest = blake2b(f"{prefix}:{symbol}:{timestamp_ms}".encode("utf-8"), digest_size=6).hexdigest()
    return f"agc{clean_prefix}{clean_symbol}{digest}"[:36]


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


def _floor_minute_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % 60_000)
