from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import polars as pl

from .continuous_identity import continuous_order_link_id, continuous_suborder_link_id
from .continuous_rebalance import ContinuousRebalanceResizePlan
from .event_demo import (
    _decimal_text,
    _float,
    _ratio_or_zero,
    _trade_return,
    order_quantity_for_notional,
)


@dataclass(frozen=True, slots=True)
class PreparedRebalanceResizeOrder:
    symbol: str
    qty: str
    planned_notional: float
    price: float
    order_link: str
    preflight: dict[str, Any]


def prepare_rebalance_resize_order(
    plan: ContinuousRebalanceResizePlan,
    trade: dict[str, Any],
    *,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    entry_link_prefix: str,
    exit_link_prefix: str,
    default_sleeve: str,
) -> PreparedRebalanceResizeOrder | None:
    symbol = str(plan.symbol or trade.get("symbol") or "")
    price = _float(price_by_symbol.get(symbol))
    if not trade or not symbol or price <= 0.0:
        return None
    contract = contract_by_symbol.get(symbol, {})
    qty_step = _float(contract.get("qty_step")) or _float(trade.get("qty_step")) or 0.001
    max_qty = _float(contract.get("max_market_order_qty")) or _float(contract.get("max_order_qty"))
    quantity = order_quantity_for_notional(
        notional_usdt=abs(plan.delta_notional_usdt),
        price=price,
        qty_step=qty_step,
        min_order_qty=_float(contract.get("min_order_qty")),
        min_notional_value=_float(contract.get("min_notional_value")),
        max_order_qty=max_qty,
    )
    if quantity is None:
        return None
    qty, planned_notional = quantity
    current_qty = abs(_float(trade.get("qty")))
    if plan.reduce_only and _float(qty) > current_qty:
        qty = _decimal_text(Decimal(str(current_qty)))
        planned_notional = current_qty * price
    if _float(qty) <= 0.0:
        return None

    link_prefix = exit_link_prefix if plan.reduce_only else entry_link_prefix
    order_link = continuous_suborder_link_id(
        link_prefix,
        symbol=symbol,
        signal_ts_ms=now_ms,
        trade_id=str(plan.trade_id),
    )
    preflight = {
        "order_link_id": order_link,
        "ts_ms": now_ms,
        "updated_at_ms": now_ms,
        "trade_id": str(plan.trade_id),
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": plan.side,
        "order_type": "Market",
        "qty": qty,
        "target_qty": qty,
        "reduce_only": bool(plan.reduce_only),
        "submit_mode": "preflight",
        "status": "submitted",
        "trade_side": "short",
        "signal_ts_ms": now_ms,
        "notional_usdt": planned_notional,
        "resize_reason": plan.reason,
        "sleeve": str(trade.get("sleeve") or default_sleeve),
    }
    if plan.reduce_only:
        preflight.update(
            {
                "exit_reason": str(plan.reason or "rebalance_reduce"),
                "exit_trigger_ts_ms": now_ms,
                "filled_qty": "",
                "error": "",
            }
        )
    return PreparedRebalanceResizeOrder(
        symbol=symbol,
        qty=qty,
        planned_notional=planned_notional,
        price=price,
        order_link=order_link,
        preflight=preflight,
    )


def build_rebalance_resize_rows(
    plans: list[ContinuousRebalanceResizePlan],
    all_trades: pl.DataFrame,
    *,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    entry_link_prefix: str,
    exit_link_prefix: str,
    default_sleeve: str,
    execution_by_trade_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build dry-run/paper ledger rows for daily-rebalance resize intents."""
    if not plans or all_trades.is_empty():
        return [], []

    lookup = {str(r.get("trade_id") or ""): r for r in all_trades.to_dicts()}
    trade_rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []

    for plan in plans:
        trade = dict(lookup.get(str(plan.trade_id), {}))
        if not trade:
            continue
        symbol = str(plan.symbol or trade.get("symbol") or "")
        execution = (execution_by_trade_id or {}).get(str(plan.trade_id), {})
        price = _float(execution.get("avg_price")) or _float(price_by_symbol.get(symbol))
        entry_price = _float(trade.get("entry_price"))
        current_qty = abs(_float(trade.get("qty")))
        equity_usdt = _float(trade.get("equity_usdt"))
        if not symbol or price <= 0.0 or entry_price <= 0.0 or current_qty <= 0.0:
            continue

        contract = contract_by_symbol.get(symbol, {})
        qty_step = _float(contract.get("qty_step")) or _float(trade.get("qty_step")) or 0.001
        if execution:
            order_qty = abs(_float(execution.get("qty")))
        else:
            max_qty = _float(contract.get("max_market_order_qty")) or _float(contract.get("max_order_qty"))
            quantity = order_quantity_for_notional(
                notional_usdt=abs(plan.delta_notional_usdt),
                price=price,
                qty_step=qty_step,
                min_order_qty=_float(contract.get("min_order_qty")),
                min_notional_value=_float(contract.get("min_notional_value")),
                max_order_qty=max_qty,
            )
            if quantity is None:
                continue

            raw_qty_text, _actual_notional = quantity
            order_qty = _float(raw_qty_text)
        if plan.reduce_only:
            order_qty = min(order_qty, current_qty)
        if order_qty <= 0.0:
            continue

        order_qty_text = _decimal_text(Decimal(str(order_qty)))
        order_notional = order_qty * price
        link_prefix = exit_link_prefix if plan.reduce_only else entry_link_prefix
        order_link = str(
            execution.get("order_link_id")
            or continuous_order_link_id(link_prefix, symbol=symbol, signal_ts_ms=now_ms)
        )
        sleeve = str(trade.get("sleeve") or default_sleeve)
        resize_reason = plan.reason or ("rebalance_reduce" if plan.reduce_only else "rebalance_increase")
        submit_mode = str(execution.get("submit_mode") or "dry_run")
        order_status = str(execution.get("status") or "planned")
        fee_usdt = _float(execution.get("fee_usdt"))
        exec_time_ms = int(_float(execution.get("exec_time_ms") or 0))
        order_id = str(execution.get("order_id") or "")
        error = str(execution.get("error") or "")

        upd = dict(trade)
        prior_realized = _float(trade.get("rebalance_realized_return"))
        if plan.reduce_only:
            realized_gross = _trade_return(entry_price, price, side="short")
            realized_weight = _ratio_or_zero(order_qty * entry_price, equity_usdt)
            realized_delta = realized_gross * realized_weight
            total_realized = prior_realized + realized_delta
            remaining_qty = max(current_qty - order_qty, 0.0)
            if remaining_qty <= qty_step * 0.5:
                upd.update(
                    {
                        "status": "closed",
                        "qty": "0",
                        "notional_usdt": 0.0,
                        "exit_ts_ms": now_ms,
                        "exit_price": price,
                        "exit_fee_usdt": fee_usdt,
                        "exit_exec_time_ms": exec_time_ms,
                        "gross_trade_return": realized_gross,
                        "net_return": total_realized,
                        "rebalance_realized_return": total_realized,
                        "exit_reason": "rebalance_zero",
                        "exit_order_link_id": order_link,
                        "exit_order_id": order_id,
                        "submit_mode": submit_mode,
                        "closed_at_ms": now_ms,
                        "updated_at_ms": now_ms,
                    }
                )
            else:
                upd.update(
                    {
                        "qty": _decimal_text(Decimal(str(remaining_qty))),
                        "notional_usdt": remaining_qty * entry_price,
                        "rebalance_realized_return": total_realized,
                        "last_rebalance_ts_ms": now_ms,
                        "last_rebalance_price": price,
                        "last_rebalance_reason": resize_reason,
                        "last_rebalance_order_link_id": order_link,
                        "last_rebalance_fee_usdt": fee_usdt,
                        "submit_mode": submit_mode,
                        "updated_at_ms": now_ms,
                    }
                )
        else:
            add_qty = order_qty
            new_qty = current_qty + add_qty
            old_cost_basis = current_qty * entry_price
            add_cost_basis = add_qty * price
            new_entry = (old_cost_basis + add_cost_basis) / new_qty
            upd.update(
                {
                    "qty": _decimal_text(Decimal(str(new_qty))),
                    "entry_price": new_entry,
                    "notional_usdt": old_cost_basis + add_cost_basis,
                    "last_rebalance_ts_ms": now_ms,
                    "last_rebalance_price": price,
                    "last_rebalance_reason": resize_reason,
                    "last_rebalance_order_link_id": order_link,
                    "last_rebalance_fee_usdt": fee_usdt,
                    "submit_mode": submit_mode,
                    "updated_at_ms": now_ms,
                }
            )

        trade_rows.append(upd)
        order_rows.append(
            {
                "order_link_id": order_link,
                "ts_ms": now_ms,
                "updated_at_ms": now_ms,
                "trade_id": str(trade.get("trade_id", "")),
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": plan.side,
                "order_type": "Market",
                "qty": order_qty_text,
                "target_qty": str(execution.get("target_qty") or order_qty_text),
                "reduce_only": bool(plan.reduce_only),
                "order_id": order_id,
                "submit_mode": submit_mode,
                "avg_price": price,
                "decision_price": price,
                "submission_price": price,
                "submitted_at_ms": now_ms,
                "fee_usdt": fee_usdt,
                "exec_time_ms": exec_time_ms,
                "notional_usdt": order_notional,
                "status": order_status,
                "trade_side": "short",
                "signal_ts_ms": now_ms,
                "equity_usdt": equity_usdt,
                "qty_step": qty_step,
                "resize_reason": resize_reason,
                "target_notional_usdt": plan.target_notional_usdt,
                "current_notional_usdt": plan.current_notional_usdt,
                "delta_notional_usdt": plan.delta_notional_usdt,
                "filled_qty": order_qty_text,
                "canonical_fill_details": execution.get("canonical_fill_details", []),
                "error": error,
                "sleeve": sleeve,
            }
        )

    return trade_rows, order_rows
