"""Exit-execution code for the event-driven demo + risk cycles.

This module owns every code path that closes a trade, reconciles a pending
exit, repairs a venue stop, or orphan-closes a ledger row whose position
has vanished from Bybit. It was extracted from event_demo.py — which had
grown to 5,700+ LOC and made cross-path audits (e.g. "does every close
path write entry_fee_usdt?") needlessly hard.

Dependency direction: this module imports from event_demo.py (the configs,
constants, and small pure helpers) but event_demo.py re-imports the public
names back at the bottom of its module so external callers
(`from liquidity_migration.event_demo import _execute_exits`) work unchanged.
"""

from __future__ import annotations

import logging
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Callable

import polars as pl

from . import _common  # noqa: F401 — kept for completeness
from ._common import MS_PER_HOUR
from .event_demo import (
    PENDING_ORDER_GUARD_MS,
    PENDING_ORDER_STATUSES,
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    _bool,
    _decimal_text,
    _empty_trades,
    _execution_summary,
    _fallback_tick_size,
    _float,
    _normalized_position_side,
    _open_trades,
    _order_params,
    _position_size_by_symbol_side,
    _prices_close,
    _quantity_text,
    _risk_order_link_id,
    _round_price,
    _safe_raw_positions,
    _safe_ratio,
    _split_order_link_id,
    _split_qty_for_max_order_size,
    _stop_price_for_entry,
    _take_profit_price_for_entry,
    _trade_return,
    _wait_for_execution_summary,
)

_logger = logging.getLogger(__name__)


def _terminalize_stale_pending_entry_orders(
    orders: pl.DataFrame,
    *,
    live_position_symbols: set[str],
    live_open_entry_order_symbols: set[str],
    now_ms: int,
) -> list[dict[str, Any]]:
    if orders.is_empty():
        return []
    rows: list[dict[str, Any]] = []
    for order in orders.to_dicts():
        if _bool(order.get("reduce_only")):
            continue
        if str(order.get("status", "")) not in PENDING_ORDER_STATUSES:
            continue
        link = str(order.get("order_link_id") or "")
        symbol = str(order.get("symbol") or "")
        trade_id = str(order.get("trade_id") or "")
        if not link or not symbol or not trade_id:
            continue
        ts_ms = int(order.get("ts_ms") or 0)
        if ts_ms <= 0 or now_ms - ts_ms <= PENDING_ORDER_GUARD_MS:
            continue
        if symbol in live_position_symbols or symbol in live_open_entry_order_symbols:
            continue
        order_update = dict(order)
        order_update.update(
            {
                "status": "expired_unconfirmed",
                "error": "stale pending entry inferred inactive from flat Bybit position and no open order",
                "updated_at_ms": now_ms,
            }
        )
        rows.append(order_update)
    return rows

def _preflight_exit_order_row(
    *,
    exit_link: str,
    now_ms: int,
    trade_id: str,
    symbol: str,
    bybit_side: str,
    order_type: str,
    qty: str,
    exit_plan: dict[str, Any],
) -> dict[str, Any]:
    """Crash-durability preflight row for an exit submission.

    Mirrors the entry preflight: a row with ``status='submitted'`` and
    ``submit_mode='preflight'`` is flushed to the orders parquet BEFORE
    ``place_order`` runs, so a crash between submission and the cycle's
    end-of-cycle flush still leaves ``exit_link`` in the ledger for the
    next cycle's ``_reconcile_pending_order_fills`` to adopt.

    Once the place_order returns, the row is overwritten by the real exit
    order row (same ``order_link_id`` key) at the cycle's ledger flush.
    """
    return {
        "order_link_id": exit_link,
        "ts_ms": now_ms,
        "trade_id": trade_id,
        "symbol": symbol,
        "side": bybit_side,
        "order_type": order_type,
        "qty": qty,
        "reduce_only": True,
        "order_id": "",
        "submit_mode": "preflight",
        "avg_price": 0.0,
        "notional_usdt": 0.0,
        "status": "submitted",
        "exit_reason": str(exit_plan.get("exit_reason") or ""),
        "exit_trigger_ts_ms": int(exit_plan.get("exit_trigger_ts_ms") or now_ms),
        "target_qty": qty,
        "filled_qty": "",
        "error": "",
    }

def _execute_exits(
    exits: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    now_ms: int,
    execution_event_router: Any | None = None,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not exits:
        return [], []
    trade_lookup = {str(row["trade_id"]): row for row in all_trades.to_dicts()} if not all_trades.is_empty() else {}
    rows: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    for exit_plan in exits:
        trade_id = str(exit_plan["trade_id"])
        trade = dict(trade_lookup.get(trade_id, {}))
        if not trade:
            continue
        symbol = str(exit_plan["symbol"])
        qty = str(exit_plan.get("qty") or trade.get("qty") or "")
        if not qty:
            continue
        side = str(exit_plan.get("side") or trade.get("side") or "short")
        bybit_side = "Buy" if side == "short" else "Sell"
        base_exit_link = _risk_order_link_id("ex", symbol=symbol, ts_ms=now_ms, attempt=0)
        # Symmetric to _execute_single_entry's split: if the position qty
        # exceeds Bybit's per-order ``maxMktOrderQty``, close it via N
        # sequential reduce-only sub-orders. Each sub is bounded by the cap;
        # the position is reduce_only so concurrent or staged fills can only
        # shrink it. Trade rows persisted before this fix lack
        # ``max_market_order_qty`` (legacy), which falls through to no split.
        target_qty_decimal = Decimal(qty)
        qty_step = _float(trade.get("qty_step")) or 0.0
        max_qty_per_order = _float(trade.get("max_market_order_qty"))
        sub_qty_decimals = _split_qty_for_max_order_size(
            target_qty=target_qty_decimal,
            max_qty_per_order=max_qty_per_order,
            qty_step=qty_step,
        )
        sub_qty_strs = [_decimal_text(q) for q in sub_qty_decimals]
        if len(sub_qty_strs) > 1:
            _logger.info(
                "exit split into %d sub-orders symbol=%s target_qty=%s "
                "max_mkt_qty=%s sub_qtys=%s",
                len(sub_qty_strs),
                symbol,
                qty,
                max_qty_per_order,
                sub_qty_strs,
            )

        def _sub_link(idx: int, _base: str = base_exit_link, _n: int = len(sub_qty_strs)) -> str:
            return _base if _n == 1 else _split_order_link_id(_base, idx)

        sub_order_rows: list[dict[str, Any]] = []
        total_filled_qty = 0.0
        total_fill_value = 0.0
        # See entry path: venue-reported fees and exec time are required for
        # reconciliation to close the demo↔Bybit PnL triangle and to measure
        # true fill-time skew.
        total_fee = 0.0
        max_exec_time_ms = 0
        first_order_id = ""
        overall_submit_mode = "dry_run"
        overall_error = ""
        any_submitted_unconfirmed = False
        any_failed = False

        for idx, sub_qty_str in enumerate(sub_qty_strs):
            sub_link = _sub_link(idx)
            sub_target = _float(sub_qty_str)
            sub_order_result: dict[str, Any] = {}
            sub_exec_summary: dict[str, Any] = {}
            sub_submit_mode = "dry_run"
            sub_status = "planned"
            sub_error = ""
            if demo.submit_orders:
                assert trading_client is not None
                if record_preflight is not None:
                    record_preflight(
                        _preflight_exit_order_row(
                            exit_link=sub_link,
                            now_ms=now_ms,
                            trade_id=trade_id,
                            symbol=symbol,
                            bybit_side=bybit_side,
                            order_type=demo.exit_order_type,
                            qty=sub_qty_str,
                            exit_plan=exit_plan,
                        )
                    )
                try:
                    sub_order_result = trading_client.place_order(
                        **_order_params(
                            symbol=symbol,
                            side=bybit_side,
                            qty=sub_qty_str,
                            order_type=demo.exit_order_type,
                            order_link_id=sub_link,
                            reduce_only=True,
                        )
                    )
                    sub_submit_mode = "submitted"
                    if not first_order_id:
                        first_order_id = sub_order_result.get("orderId", "")
                    if overall_submit_mode != "error":
                        overall_submit_mode = "submitted"
                except Exception as exc:  # noqa: BLE001 - failed exit subs are ledgered, cycle continues
                    sub_submit_mode = "error"
                    sub_status = "failed"
                    sub_error = f"place_order failed: {exc}"[:500]
                    any_failed = True
                    if idx == 0:
                        overall_submit_mode = "error"
                        overall_error = sub_error
                if sub_submit_mode == "submitted":
                    try:
                        sub_exec_summary = _wait_for_execution_summary(
                            trading_client,
                            symbol=symbol,
                            order_link_id=sub_link,
                            poll_seconds=demo.order_fill_confirm_seconds,
                            poll_interval_seconds=demo.order_fill_poll_interval_seconds,
                            fast_poll_interval_seconds=demo.order_fill_fast_poll_interval_seconds,
                            fast_poll_seconds=demo.order_fill_fast_poll_seconds,
                            execution_event_router=execution_event_router,
                        )
                    except Exception as exc:  # noqa: BLE001 - order may still fill; reconciliation will retry
                        sub_status = "submitted_unconfirmed"
                        sub_error = f"fill confirmation failed: {exc}"[:500]
                        any_submitted_unconfirmed = True
                        if idx == 0 and not overall_error:
                            overall_error = sub_error
            sub_filled_qty = _float(sub_exec_summary.get("qty")) if demo.submit_orders else sub_target
            sub_avg_price = (
                _float(sub_exec_summary.get("avg_price"))
                or _float(exit_plan.get("planned_exit_price"))
            )
            sub_fee = _float(sub_exec_summary.get("fee")) if demo.submit_orders else 0.0
            sub_exec_time_ms = int(_float(sub_exec_summary.get("exec_time_ms") or 0)) if demo.submit_orders else 0
            sub_tolerance = max(sub_target * 1e-8, 1e-12)
            if demo.submit_orders and sub_status not in {"failed", "submitted_unconfirmed"}:
                if sub_target > 0.0 and sub_filled_qty + sub_tolerance >= sub_target:
                    sub_status = "filled"
                elif sub_filled_qty > 0.0:
                    sub_status = "partial"
                    any_submitted_unconfirmed = True
                else:
                    sub_status = "submitted_unconfirmed"
                    any_submitted_unconfirmed = True
            total_filled_qty += sub_filled_qty
            if sub_filled_qty > 0.0 and sub_avg_price > 0.0:
                total_fill_value += sub_avg_price * sub_filled_qty
            total_fee += sub_fee
            if sub_exec_time_ms > max_exec_time_ms:
                max_exec_time_ms = sub_exec_time_ms
            sub_filled_str = _decimal_text(Decimal(str(sub_filled_qty))) if sub_filled_qty > 0.0 else ""
            sub_notional = abs(sub_avg_price * sub_filled_qty) if sub_filled_qty > 0.0 else 0.0
            sub_order_rows.append(
                {
                    "order_link_id": sub_link,
                    "ts_ms": now_ms,
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "side": bybit_side,
                    "order_type": demo.exit_order_type,
                    "fee_usdt": sub_fee,
                    "exec_time_ms": sub_exec_time_ms,
                    "qty": sub_qty_str,
                    "reduce_only": True,
                    "order_id": sub_order_result.get("orderId", ""),
                    "submit_mode": sub_submit_mode,
                    "avg_price": sub_avg_price,
                    "notional_usdt": sub_notional,
                    "status": sub_status if demo.submit_orders else "planned",
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "target_qty": sub_qty_str,
                    "filled_qty": sub_filled_str,
                    "error": sub_error,
                }
            )

        target_qty = _float(qty)
        exit_price = (
            (total_fill_value / total_filled_qty)
            if total_filled_qty > 0.0 and total_fill_value > 0.0
            else _float(exit_plan.get("planned_exit_price"))
        )
        qty_tolerance = max(target_qty * 1e-8, 1e-12)
        fully_filled = not demo.submit_orders or (
            target_qty > 0.0 and total_filled_qty + qty_tolerance >= target_qty
        )
        entry_price = _float(trade.get("entry_price"))
        gross_trade_return = _trade_return(entry_price, exit_price, side=side)
        notional_weight = _safe_ratio(trade.get("notional_usdt"), trade.get("equity_usdt"))
        if fully_filled:
            trade.update(
                {
                    "status": "closed",
                    "exit_ts_ms": now_ms,
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "exit_price": exit_price,
                    "exit_fee_usdt": total_fee,
                    "exit_exec_time_ms": max_exec_time_ms,
                    "gross_trade_return": gross_trade_return,
                    "net_return": gross_trade_return * notional_weight,
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_order_link_id": base_exit_link,
                    "exit_order_id": first_order_id,
                    "submit_mode": overall_submit_mode,
                    "closed_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
            )
            rows.append(trade)
        elif demo.submit_orders and total_filled_qty > 0.0:
            rows.append(
                _partial_exit_trade_update(
                    trade,
                    exit_plan,
                    filled_qty=total_filled_qty,
                    exit_price=exit_price,
                    order_link_id=base_exit_link,
                    order_id=first_order_id,
                    now_ms=now_ms,
                )
            )
        # any_failed / any_submitted_unconfirmed / overall_error feed the
        # ledger flags on individual sub-order rows above. The aggregated
        # trade-row status uses ``fully_filled`` instead, so they are not
        # re-read here.
        _ = (any_failed, any_submitted_unconfirmed, overall_error)
        orders.extend(sub_order_rows)
    return rows, orders

def _partial_exit_trade_update(
    trade: dict[str, Any],
    exit_plan: dict[str, Any],
    *,
    filled_qty: float,
    exit_price: float,
    order_link_id: str,
    order_id: str,
    now_ms: int,
) -> dict[str, Any]:
    remaining_qty = max(_float(trade.get("qty")) - filled_qty, 0.0)
    updated = dict(trade)
    updated.update(
        {
            "status": "open",
            "qty": _quantity_text(remaining_qty),
            # KNOWN LIMITATION (partial-exit accounting): the closed portion's
            # realized return is NOT booked here. Only the FINAL close writes
            # gross/net_return, weighted by the then-current (reduced) notional,
            # so a trade closed in multiple legs understates net_return by the
            # earlier legs' contribution. This single-row trade model can't carry
            # per-leg PnL; partial exits are rare (max-order-qty splits / partial
            # market fills) and the demo↔Bybit reconciliation surfaces any gap as
            # pnl_gap_usdt. Left as documented rather than refactor four close
            # paths' accounting for a rare, low-impact (demo-ledger-only) edge.
            "notional_usdt": abs(_float(trade.get("entry_price")) * remaining_qty),
            "partial_exit_order_link_id": order_link_id,
            "partial_exit_order_id": order_id,
            "partial_exit_price": exit_price,
            "partial_exit_reason": str(exit_plan.get("exit_reason") or "partial_exit"),
            "partial_exit_qty": _quantity_text(filled_qty),
            "partial_exit_trigger_ts_ms": int(exit_plan.get("exit_trigger_ts_ms") or now_ms),
            "partial_exit_ts_ms": now_ms,
            "updated_at_ms": now_ms,
        }
    )
    return updated

def _reconcile_pending_order_fills(
    orders: pl.DataFrame,
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    now_ms: int,
    live_position_symbols: set[str] | None = None,
    live_open_order_symbols: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if orders.is_empty() or trading_client is None or not demo.submit_orders:
        return [], []
    live_position_symbols = live_position_symbols or set()
    live_open_order_symbols = live_open_order_symbols or set()
    trade_lookup = {str(row["trade_id"]): row for row in all_trades.to_dicts()} if not all_trades.is_empty() else {}
    trade_rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    for order in orders.to_dicts():
        if str(order.get("status", "")) not in PENDING_ORDER_STATUSES:
            continue
        link = str(order.get("order_link_id") or "")
        symbol = str(order.get("symbol") or "")
        trade_id = str(order.get("trade_id") or "")
        if not link or not symbol or not trade_id:
            continue
        ts_ms = int(order.get("ts_ms") or 0)
        if (
            ts_ms > 0
            and now_ms - ts_ms > PENDING_ORDER_GUARD_MS
            and symbol not in live_position_symbols
            and symbol not in live_open_order_symbols
        ):
            continue
        try:
            summary = _execution_summary(trading_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50))
        except Exception as exc:  # noqa: BLE001 - keep the pending guard active and retry next cycle
            order_update = dict(order)
            order_update.update(
                {
                    "error": f"fill reconciliation failed: {exc}"[:500],
                    "updated_at_ms": now_ms,
                }
            )
            order_rows.append(order_update)
            continue
        filled_qty = _float(summary.get("qty"))
        if filled_qty <= 0.0:
            continue
        target_qty = _float(order.get("target_qty") or order.get("qty"))
        qty_tolerance = max(target_qty * 1e-8, 1e-12)
        fully_filled = target_qty > 0.0 and filled_qty + qty_tolerance >= target_qty
        avg_price = _float(summary.get("avg_price")) or _float(order.get("avg_price"))
        fee_usdt = _float(summary.get("fee"))
        exec_time_ms = int(_float(summary.get("exec_time_ms") or 0))
        # `filled_qty` from summary is the cumulative venue qty as of NOW.
        # `previous_filled_qty` is the cumulative we recorded last reconcile.
        # delta_qty = filled_qty - previous_filled_qty is the new fill. Failed
        # orders (status=failed) carry filled_qty="" and skip reconcile via the
        # `if filled_qty <= 0.0: continue` guard above, so no double-counting.
        previous_filled_qty = _float(order.get("filled_qty"))
        entry_stop_price = _float(order.get("stop_price"))
        entry_take_profit_price = _float(order.get("take_profit_price"))
        entry_stop_update_status = str(order.get("entry_stop_update_status") or "")
        entry_stop_update_error = str(order.get("entry_stop_update_error") or "")
        if not _bool(order.get("reduce_only")) and avg_price > 0.0:
            trade_side = str(order.get("trade_side") or ("short" if str(order.get("side", "")) == "Sell" else "long"))
            tick_size = _float(order.get("tick_size")) or 0.0001
            stop_loss_pct = _float(order.get("stop_loss_pct"))
            take_profit_pct = _float(order.get("take_profit_pct"))
            recalculated_stop_price = (
                _stop_price_for_entry(entry_price=avg_price, side=trade_side, stop_loss_pct=stop_loss_pct, tick_size=tick_size)
                if stop_loss_pct > 0.0
                else entry_stop_price
            )
            recalculated_take_profit_price = (
                _take_profit_price_for_entry(
                    entry_price=avg_price,
                    side=trade_side,
                    take_profit_pct=take_profit_pct,
                    tick_size=tick_size,
                )
                if take_profit_pct > 0.0
                else entry_take_profit_price
            )
            if (stop_loss_pct > 0.0 or take_profit_pct > 0.0) and (
                not _prices_close(entry_stop_price, recalculated_stop_price, tolerance_bps=0.0)
                or (
                    recalculated_take_profit_price > 0.0
                    and not _prices_close(entry_take_profit_price, recalculated_take_profit_price, tolerance_bps=0.0)
                )
            ):
                try:
                    trading_client.set_trading_stop(
                        symbol=symbol,
                        stop_loss=_decimal_text(Decimal(str(recalculated_stop_price)))
                        if recalculated_stop_price > 0.0
                        else None,
                        take_profit=_decimal_text(Decimal(str(recalculated_take_profit_price)))
                        if recalculated_take_profit_price > 0.0
                        else None,
                    )
                    entry_stop_update_status = "submitted"
                    entry_stop_update_error = ""
                except Exception as exc:  # noqa: BLE001 - venue repair daemon will retry from ledger state
                    entry_stop_update_status = "failed"
                    entry_stop_update_error = str(exc)[:500]
            entry_stop_price = recalculated_stop_price
            entry_take_profit_price = recalculated_take_profit_price
        order_update = dict(order)
        order_update.update(
            {
                "status": "filled" if fully_filled else "partial",
                "filled_qty": _decimal_text(Decimal(str(filled_qty))),
                "avg_price": avg_price,
                "fee_usdt": fee_usdt,
                "exec_time_ms": exec_time_ms,
                "notional_usdt": abs(avg_price * filled_qty) if avg_price > 0.0 else 0.0,
                "stop_price": entry_stop_price,
                "take_profit_price": entry_take_profit_price,
                "entry_stop_update_status": entry_stop_update_status,
                "entry_stop_update_error": entry_stop_update_error,
                "updated_at_ms": now_ms,
            }
        )
        order_rows.append(order_update)
        if _bool(order.get("reduce_only")):
            trade = dict(trade_lookup.get(trade_id, {}))
            if not trade or str(trade.get("status")) == "closed":
                continue
            delta_qty = max(filled_qty - previous_filled_qty, 0.0)
            remaining_qty = max(_float(trade.get("qty")) - delta_qty, 0.0)
            if fully_filled or remaining_qty <= max(_float(trade.get("qty")) * 1e-8, 1e-12):
                # gross_trade_return / net_return must land on the close so the
                # ledger carries realized PnL without depending on the orphan
                # reconciler. Both fields use the same formula as the cycle-exit
                # path (lines ~3525) and orphan backfill (lines ~4778).
                trade_side = str(trade.get("side") or "short")
                entry_price = _float(trade.get("entry_price"))
                gross_trade_return = _trade_return(entry_price, avg_price, side=trade_side)
                notional_weight = _safe_ratio(trade.get("notional_usdt"), trade.get("equity_usdt"))
                trade.update(
                    {
                        "status": "closed",
                        "exit_ts_ms": now_ms,
                        "exit_trigger_ts_ms": int(order.get("exit_trigger_ts_ms") or now_ms),
                        "exit_price": avg_price,
                        "exit_fee_usdt": fee_usdt,
                        "exit_exec_time_ms": exec_time_ms,
                        "gross_trade_return": gross_trade_return,
                        "net_return": gross_trade_return * notional_weight,
                        "exit_reason": str(order.get("exit_reason") or "pending_exit_fill"),
                        "exit_order_link_id": link,
                        "exit_order_id": order.get("order_id", ""),
                        "submit_mode": str(order.get("submit_mode") or "execution_reconciled"),
                        "closed_at_ms": now_ms,
                        "updated_at_ms": now_ms,
                    }
                )
                trade_rows.append(trade)
            elif delta_qty > 0.0:
                trade.update(
                    {
                        "qty": _decimal_text(Decimal(str(remaining_qty))),
                        "notional_usdt": abs(_float(trade.get("entry_price")) * remaining_qty),
                        "partial_exit_order_link_id": link,
                        "partial_exit_price": avg_price,
                        "partial_exit_reason": str(order.get("exit_reason") or "pending_exit_partial_fill"),
                        "updated_at_ms": now_ms,
                    }
                )
                trade_rows.append(trade)
            continue
        existing_trade = dict(trade_lookup.get(trade_id, {}))
        if existing_trade:
            if str(existing_trade.get("status")) != "closed" and filled_qty > _float(existing_trade.get("qty")):
                leverage = _float(existing_trade.get("entry_leverage")) or _float(order.get("entry_leverage")) or demo.entry_leverage
                notional = abs(avg_price * filled_qty) if avg_price > 0.0 else _float(existing_trade.get("notional_usdt"))
                initial_margin = notional / leverage if leverage > 0.0 else 0.0
                equity = _float(existing_trade.get("equity_usdt"))
                existing_trade.update(
                    {
                        "entry_price": avg_price,
                        "qty": _decimal_text(Decimal(str(filled_qty))),
                        "notional_usdt": notional,
                        "initial_margin_usdt": initial_margin,
                        "initial_margin_pct_equity": initial_margin / equity if equity > 0.0 else 0.0,
                        "stop_price": entry_stop_price,
                        "take_profit_price": entry_take_profit_price,
                        "entry_stop_update_status": entry_stop_update_status,
                        "entry_stop_update_error": entry_stop_update_error,
                        "updated_at_ms": now_ms,
                    }
                )
                trade_rows.append(existing_trade)
            continue
        leverage = _float(order.get("entry_leverage")) or demo.entry_leverage
        notional = abs(avg_price * filled_qty) if avg_price > 0.0 else _float(order.get("notional_usdt"))
        initial_margin = notional / leverage if leverage > 0.0 else 0.0
        equity = _float(order.get("equity_usdt"))
        bybit_side = str(order.get("side", ""))
        trade_side = str(order.get("trade_side") or ("short" if bybit_side == "Sell" else "long"))
        opened_at_ms = int(order.get("ts_ms") or now_ms)
        trade_rows.append(
            {
                "trade_id": trade_id,
                "symbol": symbol,
                "side": trade_side,
                "signal_ts_ms": int(order.get("signal_ts_ms") or opened_at_ms),
                "ts_ms": now_ms,
                "status": "open",
                "entry_ts_ms": opened_at_ms,
                "entry_exec_time_ms": exec_time_ms,
                "entry_fee_usdt": fee_usdt,
                "entry_price": avg_price,
                "qty": _decimal_text(Decimal(str(filled_qty))),
                "notional_usdt": notional,
                "equity_usdt": equity,
                "target_notional_pct_equity": _float(order.get("target_notional_pct_equity")),
                "entry_leverage": leverage,
                "initial_margin_usdt": initial_margin,
                "initial_margin_pct_equity": initial_margin / equity if equity > 0.0 else 0.0,
                "tick_size": _float(order.get("tick_size")),
                "qty_step": _float(order.get("qty_step")),
                "stop_price": entry_stop_price,
                "take_profit_price": entry_take_profit_price,
                "entry_stop_update_status": entry_stop_update_status,
                "entry_stop_update_error": entry_stop_update_error,
                "entry_order_link_id": link,
                "entry_order_id": order.get("order_id", ""),
                "submit_mode": "execution_reconciled",
                "opened_at_ms": opened_at_ms,
                "updated_at_ms": now_ms,
            }
        )
    return trade_rows, order_rows

def _execute_risk_exits(
    exits: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    risk: EventRiskCycleConfig,
    now_ms: int,
    price_by_symbol: dict[str, float],
    tick_size_by_symbol: dict[str, float],
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not exits:
        return [], []
    trade_lookup = {str(row["trade_id"]): row for row in all_trades.to_dicts()} if not all_trades.is_empty() else {}
    rows: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    for exit_plan in exits:
        trade_id = str(exit_plan["trade_id"])
        trade = dict(trade_lookup.get(trade_id, {}))
        if not trade:
            continue
        symbol = str(exit_plan["symbol"])
        qty = str(exit_plan.get("qty") or trade.get("qty") or "")
        if not qty:
            continue
        side = str(exit_plan.get("side") or trade.get("side") or "short")
        bybit_side = "Buy" if side == "short" else "Sell"
        planned_price = _float(exit_plan.get("planned_exit_price")) or price_by_symbol.get(symbol, 0.0)
        # Tag the preflight row with the trade context so the next-cycle
        # reconciler can route the resolved fill back to the right trade.
        def _record_with_context(row: dict[str, Any], _trade_id: str = trade_id, _exit_plan: dict[str, Any] = exit_plan) -> None:
            if record_preflight is None:
                return
            tagged = dict(row)
            tagged["trade_id"] = _trade_id
            tagged["exit_reason"] = str(_exit_plan.get("exit_reason") or "")
            tagged["exit_trigger_ts_ms"] = int(_exit_plan.get("exit_trigger_ts_ms") or now_ms)
            record_preflight(tagged)
        try:
            submit = _submit_reduce_only_exit(
                symbol=symbol,
                bybit_side=bybit_side,
                qty=qty,
                trading_client=trading_client,
                risk=risk,
                now_ms=now_ms,
                reference_price=planned_price,
                tick_size=tick_size_by_symbol.get(symbol) or _float(trade.get("tick_size")) or 0.0,
                # Trade rows persisted before the 2026-05-27 split work lack
                # ``max_market_order_qty`` (legacy ledger) — the missing
                # value falls through to no split in
                # _split_qty_for_max_order_size, preserving prior behaviour.
                max_qty_per_order=_float(trade.get("max_market_order_qty")),
                qty_step=_float(trade.get("qty_step")),
                record_preflight=_record_with_context if record_preflight is not None else None,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in order telemetry so the loop can continue
            link = _risk_order_link_id("rx", symbol=symbol, ts_ms=now_ms, attempt=0)
            failed_order = _risk_order_row(
                link=link,
                ts_ms=now_ms,
                symbol=symbol,
                side=bybit_side,
                qty=qty,
                order_type="Market" if risk.exit_order_mode == "market" else "LimitChase",
                submit_mode="error",
                status="failed",
                error=str(exc)[:500],
            )
            failed_order.update(
                {
                    "trade_id": trade_id,
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "avg_price": planned_price,
                    "target_qty": qty,
                    "filled_qty": "",
                    "notional_usdt": 0.0,
                }
            )
            orders.append(failed_order)
            continue
        exit_price = _float(submit["exec_summary"].get("avg_price")) or planned_price
        target_qty = _float(qty)
        filled_qty = _float(submit["exec_summary"].get("qty"))
        exit_fee_usdt = _float(submit["exec_summary"].get("fee"))
        exit_exec_time_ms = int(_float(submit["exec_summary"].get("exec_time_ms") or 0))
        qty_tolerance = max(target_qty * 1e-8, 1e-12)
        fully_filled = not risk.submit_orders or (target_qty > 0.0 and filled_qty + qty_tolerance >= target_qty)
        for order_row in submit["order_rows"]:
            row_target_qty = str(order_row.get("target_qty") or order_row.get("qty") or qty)
            row_filled_qty = _float(order_row.get("filled_qty"))
            row_status = str(order_row.get("status") or "")
            if risk.submit_orders and row_status in {"", "submitted"}:
                row_target_float = _float(row_target_qty)
                row_tolerance = max(row_target_float * 1e-8, 1e-12)
                order_row["status"] = (
                    "filled"
                    if row_target_float > 0.0 and row_filled_qty + row_tolerance >= row_target_float
                    else "partial"
                    if row_filled_qty > 0.0
                    else "submitted_unconfirmed"
                )
            row_avg_price = _float(order_row.get("avg_price")) or exit_price
            row_fee = _float(order_row.get("fee_usdt"))
            row_exec_time_ms = int(_float(order_row.get("exec_time_ms") or 0))
            notional_qty = row_filled_qty if risk.submit_orders else _float(row_target_qty)
            order_row.update(
                {
                    "trade_id": trade_id,
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "avg_price": row_avg_price,
                    "fee_usdt": row_fee,
                    "exec_time_ms": row_exec_time_ms,
                    "filled_qty": _decimal_text(Decimal(str(row_filled_qty))) if row_filled_qty > 0.0 else "",
                    "target_qty": row_target_qty,
                    "notional_usdt": abs(row_avg_price * notional_qty) if row_avg_price > 0.0 else 0.0,
                }
            )
            orders.append(order_row)
        if fully_filled:
            # Mirror the cycle-exit and pending-exit-reconcile paths: a closed
            # trade must carry both gross_trade_return and net_return so the
            # orphan reconciler does not have to backfill them post-hoc.
            trade_side = str(trade.get("side") or "short")
            entry_price = _float(trade.get("entry_price"))
            gross_trade_return = _trade_return(entry_price, exit_price, side=trade_side)
            notional_weight = _safe_ratio(trade.get("notional_usdt"), trade.get("equity_usdt"))
            trade.update(
                {
                    "status": "closed",
                    "exit_ts_ms": now_ms,
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "exit_price": exit_price,
                    "exit_fee_usdt": exit_fee_usdt,
                    "exit_exec_time_ms": exit_exec_time_ms,
                    "gross_trade_return": gross_trade_return,
                    "net_return": gross_trade_return * notional_weight,
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_order_link_id": submit["order_link_id"],
                    "exit_order_id": submit["order_id"],
                    "submit_mode": submit["submit_mode"],
                    "closed_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
            )
            rows.append(trade)
        elif risk.submit_orders and filled_qty > 0.0:
            rows.append(
                _partial_exit_trade_update(
                    trade,
                    exit_plan,
                    filled_qty=filled_qty,
                    exit_price=exit_price,
                    order_link_id=submit["order_link_id"],
                    order_id=submit["order_id"],
                    now_ms=now_ms,
                )
            )
    return rows, orders

def _execute_stop_repairs(
    repairs: list[dict[str, Any]],
    *,
    trading_client: Any | None,
    risk: EventRiskCycleConfig,
    now_ms: int,
) -> list[dict[str, Any]]:
    if not repairs or not risk.repair_stops:
        return []
    rows: list[dict[str, Any]] = []
    for repair in repairs:
        symbol = str(repair["symbol"])
        link = _risk_order_link_id("st", symbol=symbol, ts_ms=now_ms, attempt=len(rows))
        submit_mode = "dry_run"
        status = "planned"
        error = ""
        if risk.submit_orders:
            assert trading_client is not None
            try:
                trading_client.set_trading_stop(
                    symbol=symbol,
                    stop_loss=_decimal_text(Decimal(str(repair["stop_price"])))
                    if _float(repair.get("stop_price")) > 0.0
                    else None,
                    take_profit=_decimal_text(Decimal(str(repair["take_profit_price"])))
                    if _float(repair.get("take_profit_price")) > 0.0
                    else None,
                )
                submit_mode = "submitted"
                status = "stop_repaired"
            except Exception as exc:  # noqa: BLE001 - surfaced in cycle telemetry
                submit_mode = "error"
                status = "failed"
                error = str(exc)[:500]
        rows.append(
            {
                "order_link_id": link,
                "ts_ms": now_ms,
                "trade_id": str(repair.get("trade_id", "")),
                "symbol": symbol,
                "side": "",
                "order_type": "TradingStop",
                "qty": "",
                "reduce_only": True,
                "order_id": "",
                "submit_mode": submit_mode,
                "avg_price": 0.0,
                "notional_usdt": 0.0,
                "status": status,
                "exit_reason": "",
                "stop_price": _float(repair.get("stop_price")),
                "take_profit_price": _float(repair.get("take_profit_price")),
                "error": error,
            }
        )
    return rows

def _risk_preflight_order_row(
    *,
    link: str,
    ts_ms: int,
    symbol: str,
    side: str,
    qty: str,
    order_type: str,
) -> dict[str, Any]:
    """Crash-durability preflight row for a wsrisk reduce-only exit submission.

    Mirrors _preflight_exit_order_row on the main cycle: status='submitted' +
    submit_mode='preflight' written BEFORE place_order so a crash between
    submission and the cycle's end-of-cycle flush still leaves the order_link_id
    in parquet for next-cycle pending-fill reconciliation.
    """
    row = _risk_order_row(
        link=link,
        ts_ms=ts_ms,
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=order_type,
        submit_mode="preflight",
        status="submitted",
    )
    row.update({"target_qty": qty, "filled_qty": ""})
    return row

def _submit_reduce_only_exit(
    *,
    symbol: str,
    bybit_side: str,
    qty: str,
    trading_client: Any | None,
    risk: EventRiskCycleConfig,
    now_ms: int,
    reference_price: float,
    tick_size: float,
    max_qty_per_order: float = 0.0,
    qty_step: float = 0.0,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not risk.submit_orders:
        link = _risk_order_link_id("rx", symbol=symbol, ts_ms=now_ms, attempt=0)
        return {
            "order_link_id": link,
            "order_id": "",
            "submit_mode": "dry_run",
            "exec_summary": {"qty": "", "avg_price": 0.0, "fee": 0.0, "executions": 0},
            "order_rows": [
                _risk_order_row(
                    link=link,
                    ts_ms=now_ms,
                    symbol=symbol,
                    side=bybit_side,
                    qty=qty,
                    order_type="Market" if risk.exit_order_mode == "market" else "LimitChase",
                    submit_mode="dry_run",
                    status="planned",
                )
            ],
        }
    assert trading_client is not None
    if risk.exit_order_mode == "market":
        base_link = _risk_order_link_id("rx", symbol=symbol, ts_ms=now_ms, attempt=0)
        # Symmetric to the entry-side split: a reduce-only close that
        # exceeds Bybit's per-order ``maxMktOrderQty`` is rejected outright,
        # so split the close into N sub-orders each ≤ cap. Without max_qty
        # info (legacy callers / lazy fixture) the split helper returns the
        # original qty unchanged, preserving prior behaviour.
        target_qty_decimal = Decimal(qty)
        sub_qty_decimals = _split_qty_for_max_order_size(
            target_qty=target_qty_decimal,
            max_qty_per_order=max_qty_per_order,
            qty_step=qty_step,
        )
        sub_qty_strs = [_decimal_text(q) for q in sub_qty_decimals]
        if len(sub_qty_strs) > 1:
            _logger.info(
                "ws-risk exit split into %d sub-orders symbol=%s target_qty=%s "
                "max_mkt_qty=%s sub_qtys=%s",
                len(sub_qty_strs),
                symbol,
                qty,
                max_qty_per_order,
                sub_qty_strs,
            )

        def _sub_link(idx: int, _base: str = base_link, _n: int = len(sub_qty_strs)) -> str:
            return _base if _n == 1 else _split_order_link_id(_base, idx)

        order_rows: list[dict[str, Any]] = []
        total_filled_qty = 0.0
        total_fill_value = 0.0
        first_order_id = ""
        any_submitted_unconfirmed = False
        last_error = ""
        for idx, sub_qty_str in enumerate(sub_qty_strs):
            sub_link = _sub_link(idx)
            sub_target = _float(sub_qty_str)
            if record_preflight is not None:
                record_preflight(
                    _risk_preflight_order_row(
                        link=sub_link,
                        ts_ms=now_ms,
                        symbol=symbol,
                        side=bybit_side,
                        qty=sub_qty_str,
                        order_type="Market",
                    )
                )
            try:
                sub_order_result = trading_client.place_order(
                    **_order_params(
                        symbol=symbol,
                        side=bybit_side,
                        qty=sub_qty_str,
                        order_type="Market",
                        order_link_id=sub_link,
                        reduce_only=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - failed reduce-only sub-order is surfaced to caller for retry
                # First-sub place_order failure: surface as a hard error to
                # the caller (matches pre-split single-order behaviour where
                # a place_order exception propagated out of this helper).
                if idx == 0:
                    raise
                sub_err = f"place_order failed: {exc}"[:500]
                last_error = sub_err
                sub_row = _risk_order_row(
                    link=sub_link,
                    ts_ms=now_ms,
                    symbol=symbol,
                    side=bybit_side,
                    qty=sub_qty_str,
                    order_type="Market",
                    submit_mode="error",
                    status="failed",
                    error=sub_err,
                )
                sub_row.update({"target_qty": sub_qty_str, "filled_qty": ""})
                order_rows.append(sub_row)
                continue
            sub_error = ""
            sub_status = "submitted"
            try:
                sub_exec_summary = _execution_summary(
                    trading_client.get_trade_history(symbol=symbol, order_link_id=sub_link, limit=50)
                )
            except Exception as exc:  # noqa: BLE001 - accepted reduce-only order remains pending for reconciliation
                sub_exec_summary = {"qty": "", "avg_price": 0.0, "fee": 0.0, "executions": 0}
                sub_status = "submitted_unconfirmed"
                sub_error = f"fill confirmation failed: {exc}"[:500]
                last_error = sub_error
                any_submitted_unconfirmed = True
            sub_filled_qty = _float(sub_exec_summary.get("qty"))
            if sub_status != "submitted_unconfirmed":
                if sub_target > 0.0 and sub_filled_qty + max(sub_target * 1e-8, 1e-12) >= sub_target:
                    sub_status = "filled"
                elif sub_filled_qty > 0.0:
                    sub_status = "partial"
                    any_submitted_unconfirmed = True
                else:
                    sub_status = "submitted_unconfirmed"
                    any_submitted_unconfirmed = True
            sub_avg_price = _float(sub_exec_summary.get("avg_price"))
            total_filled_qty += sub_filled_qty
            if sub_filled_qty > 0.0 and sub_avg_price > 0.0:
                total_fill_value += sub_avg_price * sub_filled_qty
            if not first_order_id:
                first_order_id = sub_order_result.get("orderId", "")
            sub_row = _risk_order_row(
                link=sub_link,
                ts_ms=now_ms,
                symbol=symbol,
                side=bybit_side,
                qty=sub_qty_str,
                order_type="Market",
                submit_mode="submitted",
                status=sub_status,
                order_id=sub_order_result.get("orderId", ""),
                error=sub_error,
            )
            sub_row.update(
                {
                    "target_qty": sub_qty_str,
                    "filled_qty": _decimal_text(Decimal(str(sub_filled_qty))) if sub_filled_qty > 0.0 else "",
                    "avg_price": sub_avg_price,
                    "notional_usdt": abs(sub_avg_price * sub_filled_qty) if sub_avg_price > 0.0 else 0.0,
                }
            )
            order_rows.append(sub_row)

        target_qty = _float(qty)
        avg_price = (
            (total_fill_value / total_filled_qty)
            if total_filled_qty > 0.0 and total_fill_value > 0.0
            else 0.0
        )
        agg_summary: dict[str, Any] = {
            "qty": _decimal_text(Decimal(str(total_filled_qty))) if total_filled_qty > 0.0 else "",
            "avg_price": avg_price,
            "fee": 0.0,
            "executions": sum(1 for r in order_rows if _float(r.get("filled_qty")) > 0.0),
        }
        _ = (any_submitted_unconfirmed, last_error, target_qty)
        return {
            "order_link_id": base_link,
            "order_id": first_order_id,
            "submit_mode": "submitted",
            "exec_summary": agg_summary,
            "order_rows": order_rows,
        }
    return _submit_limit_chase_exit(
        symbol=symbol,
        bybit_side=bybit_side,
        qty=qty,
        trading_client=trading_client,
        risk=risk,
        now_ms=now_ms,
        reference_price=reference_price,
        tick_size=tick_size,
        record_preflight=record_preflight,
    )

def _submit_limit_chase_exit(
    *,
    symbol: str,
    bybit_side: str,
    qty: str,
    trading_client: Any,
    risk: EventRiskCycleConfig,
    now_ms: int,
    reference_price: float,
    tick_size: float,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    target_qty = _float(qty)
    filled_qty = 0.0
    executions: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    last_link = ""
    last_order_id = ""
    attempts = max(1, risk.limit_chase_attempts)
    for attempt in range(attempts):
        remaining_qty = max(target_qty - filled_qty, 0.0)
        if remaining_qty <= max(target_qty * 1e-8, 1e-12):
            break
        link = _risk_order_link_id("lc", symbol=symbol, ts_ms=now_ms, attempt=attempt)
        last_link = link
        bps = min(risk.limit_chase_max_bps, risk.limit_chase_initial_bps + attempt * risk.limit_chase_step_bps)
        limit_price = _limit_chase_price(bybit_side=bybit_side, reference_price=reference_price, bps=bps, tick_size=tick_size)
        if record_preflight is not None:
            record_preflight(
                _risk_preflight_order_row(
                    link=link,
                    ts_ms=now_ms,
                    symbol=symbol,
                    side=bybit_side,
                    qty=_decimal_text(Decimal(str(remaining_qty))),
                    order_type="Limit",
                )
            )
        order_result = trading_client.place_order(
            **_order_params(
                symbol=symbol,
                side=bybit_side,
                qty=_decimal_text(Decimal(str(remaining_qty))),
                order_type="Limit",
                order_link_id=link,
                reduce_only=True,
                price=limit_price,
                time_in_force="IOC",
            )
        )
        last_order_id = order_result.get("orderId", "")
        if risk.limit_chase_wait_seconds > 0.0:
            time.sleep(risk.limit_chase_wait_seconds)
        try:
            batch = trading_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50)
        except Exception as exc:  # noqa: BLE001 - accepted IOC may still fill; do not chase blind
            remaining_qty_text = _quantity_text(remaining_qty)
            row = _risk_order_row(
                link=link,
                ts_ms=now_ms,
                symbol=symbol,
                side=bybit_side,
                qty=remaining_qty_text,
                order_type="Limit",
                submit_mode="submitted",
                status="submitted_unconfirmed",
                order_id=last_order_id,
                price=limit_price,
                time_in_force="IOC",
                error=f"fill confirmation failed: {exc}"[:500],
            )
            row.update({"target_qty": remaining_qty_text, "filled_qty": ""})
            order_rows.append(row)
            return {
                "order_link_id": last_link,
                "order_id": last_order_id,
                "submit_mode": "submitted",
                "exec_summary": _execution_summary(executions),
                "order_rows": order_rows,
            }
        summary = _execution_summary(batch)
        order_filled_qty = _float(summary.get("qty"))
        filled_qty += order_filled_qty
        executions.extend(batch)
        remaining_qty_text = _quantity_text(remaining_qty)
        order_avg_price = _float(summary.get("avg_price"))
        row_status = (
            "filled"
            if remaining_qty > 0.0 and order_filled_qty + max(remaining_qty * 1e-8, 1e-12) >= remaining_qty
            else "partial"
            if order_filled_qty > 0.0
            else "unfilled"
        )
        row = _risk_order_row(
            link=link,
            ts_ms=now_ms,
            symbol=symbol,
            side=bybit_side,
            qty=remaining_qty_text,
            order_type="Limit",
            submit_mode="submitted",
            status=row_status,
            order_id=last_order_id,
            price=limit_price,
            time_in_force="IOC",
        )
        row.update(
            {
                "target_qty": remaining_qty_text,
                "filled_qty": _decimal_text(Decimal(str(order_filled_qty))) if order_filled_qty > 0.0 else "",
                "avg_price": order_avg_price,
                "notional_usdt": abs(order_avg_price * order_filled_qty) if order_avg_price > 0.0 else 0.0,
            }
        )
        order_rows.append(row)
    remaining_qty = max(target_qty - filled_qty, 0.0)
    if remaining_qty > max(target_qty * 1e-8, 1e-12) and risk.limit_chase_fallback_market:
        link = _risk_order_link_id("lm", symbol=symbol, ts_ms=now_ms, attempt=attempts)
        last_link = link
        remaining_qty_text = _quantity_text(remaining_qty)
        if record_preflight is not None:
            record_preflight(
                _risk_preflight_order_row(
                    link=link,
                    ts_ms=now_ms,
                    symbol=symbol,
                    side=bybit_side,
                    qty=remaining_qty_text,
                    order_type="Market",
                )
            )
        order_result = trading_client.place_order(
            **_order_params(
                symbol=symbol,
                side=bybit_side,
                qty=remaining_qty_text,
                order_type="Market",
                order_link_id=link,
                reduce_only=True,
            )
        )
        last_order_id = order_result.get("orderId", "")
        error = ""
        status = "fallback_market"
        summary = {"qty": "", "avg_price": 0.0, "fee": 0.0, "executions": 0}
        try:
            batch = trading_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50)
            executions.extend(batch)
            summary = _execution_summary(batch)
        except Exception as exc:  # noqa: BLE001 - accepted market fallback remains pending for reconciliation
            status = "submitted_unconfirmed"
            error = f"fill confirmation failed: {exc}"[:500]
        order_filled_qty = _float(summary.get("qty"))
        if status != "submitted_unconfirmed":
            status = (
                "filled"
                if remaining_qty > 0.0 and order_filled_qty + max(remaining_qty * 1e-8, 1e-12) >= remaining_qty
                else "partial"
                if order_filled_qty > 0.0
                else "fallback_market"
            )
        order_avg_price = _float(summary.get("avg_price"))
        row = _risk_order_row(
            link=link,
            ts_ms=now_ms,
            symbol=symbol,
            side=bybit_side,
            qty=remaining_qty_text,
            order_type="Market",
            submit_mode="submitted",
            status=status,
            order_id=last_order_id,
            error=error,
        )
        row.update(
            {
                "target_qty": remaining_qty_text,
                "filled_qty": _decimal_text(Decimal(str(order_filled_qty))) if order_filled_qty > 0.0 else "",
                "avg_price": order_avg_price,
                "notional_usdt": abs(order_avg_price * order_filled_qty) if order_avg_price > 0.0 else 0.0,
            }
        )
        order_rows.append(row)
    return {
        "order_link_id": last_link,
        "order_id": last_order_id,
        "submit_mode": "submitted",
        "exec_summary": _execution_summary(executions),
        "order_rows": order_rows,
    }

def _reconcile_open_trades(
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    now_ms: int,
    raw_positions: list[dict[str, Any]] | None = None,
    position_error: str = "",
) -> tuple[pl.DataFrame, list[dict[str, Any]], str]:
    open_trades = _open_trades(all_trades)
    if open_trades.is_empty() or trading_client is None or not demo.submit_orders:
        return open_trades, [], ""
    if raw_positions is None and not position_error:
        raw_positions, position_error = _safe_raw_positions(trading_client, settle_coin=demo.settle_coin)
    positions = raw_positions or []
    error = position_error
    if error:
        return open_trades, [], error
    size_by_symbol_side = _position_size_by_symbol_side(positions)
    updates: list[dict[str, Any]] = []
    kept = []
    for trade in open_trades.to_dicts():
        symbol = str(trade["symbol"])
        # Normalize through the same helper so "short" / "Sell" both land
        # on "short" — trade rows carry "short"/"long" and Bybit positions
        # carry "Sell"/"Buy", but the lookup must agree.
        trade_side = _normalized_position_side(trade.get("side"))
        if trade_side and size_by_symbol_side.get((symbol, trade_side), 0.0) > 0.0:
            kept.append(trade)
            continue
        updates.append(_orphan_close_trade_row(trade, now_ms=now_ms, trading_client=trading_client))
    return pl.DataFrame(kept, infer_schema_length=None) if kept else _empty_trades(), updates, ""

def _risk_reconcile_missing_positions(
    open_trades: pl.DataFrame,
    *,
    position_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    enabled: bool,
    position_error: str = "",
    trading_client: Any | None = None,
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Close ledger rows whose Bybit position has vanished.

    Skipped when ``position_error`` is set: a failed ``get_positions`` returns an
    empty ``position_by_symbol`` that is indistinguishable from "no positions",
    so without this guard every open trade would be false-positive orphan-closed
    on a single transient API failure. The caller plumbs the error string from
    :func:`_safe_raw_positions`.

    When a ``trading_client`` is provided, queries ``get_closed_pnl`` per orphan
    symbol to backfill ``exit_price`` / ``gross_trade_return`` / ``net_return``
    / ``exit_order_id`` / ``exit_ts_ms`` from the actual close. Missing PnL data
    is non-fatal -- the trade still closes with ``exit_reason='bybit_position_missing'``
    and the previous zero-PnL defaults.
    """
    if open_trades.is_empty() or not enabled:
        return open_trades, []
    if position_error:
        return open_trades, []
    updates: list[dict[str, Any]] = []
    kept = []
    for trade in open_trades.to_dicts():
        symbol = str(trade.get("symbol", ""))
        if symbol and symbol in position_by_symbol:
            kept.append(trade)
            continue
        updates.append(_orphan_close_trade_row(trade, now_ms=now_ms, trading_client=trading_client))
    return pl.DataFrame(kept, infer_schema_length=None) if kept else _empty_trades(), updates

def _orphan_close_trade_row(
    trade: dict[str, Any],
    *,
    now_ms: int,
    trading_client: Any | None,
) -> dict[str, Any]:
    """Build an orphan-close trade row, backfilling PnL from Bybit when possible.

    Falls back to the legacy zero-PnL row on any failure (missing endpoint,
    transport error, no matching record). The reconciler must always be able to
    close the ledger row -- a backfill failure cannot block the close.
    """
    updated = dict(trade)
    updated.update(
        {
            "status": "closed",
            "exit_ts_ms": now_ms,
            "exit_trigger_ts_ms": now_ms,
            "exit_reason": "bybit_position_missing",
            "closed_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
    )
    backfill = _orphan_close_pnl_backfill(trade, now_ms=now_ms, trading_client=trading_client)
    if backfill:
        updated.update(backfill)
    return updated

def _orphan_close_pnl_backfill(
    trade: dict[str, Any],
    *,
    now_ms: int,
    trading_client: Any | None,
) -> dict[str, Any]:
    """Query Bybit closed-PnL for an orphan trade and return backfill fields.

    Returns an empty dict on any failure -- the caller keeps the zero-PnL defaults.
    """
    if trading_client is None:
        return {}
    symbol = str(trade.get("symbol", ""))
    if not symbol:
        return {}
    side = str(trade.get("side") or "short")
    entry_ts_ms = int(trade.get("entry_ts_ms") or 0)
    entry_price = _float(trade.get("entry_price"))
    if entry_price <= 0.0:
        return {}
    get_closed_pnl = getattr(trading_client, "get_closed_pnl", None)
    if not callable(get_closed_pnl):
        return {}
    # Pull the most recent closures for this symbol. Bybit returns up to 200
    # records per call; the default limit=50 is more than enough to cover the
    # closures since the trade opened on any realistic cycle cadence.
    start_time_ms = max(entry_ts_ms - MS_PER_HOUR, 0) if entry_ts_ms > 0 else None
    try:
        records = get_closed_pnl(symbol=symbol, start_time_ms=start_time_ms, limit=50)
    except Exception:  # noqa: BLE001 - reconciler must close the row even when backfill fails
        return {}
    if not records:
        return {}
    # Close side: for our short trade the closing order is Buy; for long it is Sell.
    expected_close_side = "Buy" if side == "short" else "Sell"
    candidates: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        record_side = str(record.get("side") or "")
        if record_side and record_side != expected_close_side:
            continue
        # Bybit returns createdTime / updatedTime as ms-since-epoch strings or ints.
        created_ts = int(_float(record.get("createdTime") or record.get("updatedTime") or 0))
        if entry_ts_ms > 0 and created_ts > 0 and created_ts < entry_ts_ms:
            continue
        candidates.append((created_ts, record))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, best = candidates[0]
    exit_price = _float(best.get("avgExitPrice"))
    if exit_price <= 0.0:
        return {}
    gross_trade_return = _trade_return(entry_price, exit_price, side=side)
    notional_weight = _safe_ratio(trade.get("notional_usdt"), trade.get("equity_usdt"))
    closed_at_ms = int(_float(best.get("createdTime") or best.get("updatedTime") or now_ms)) or now_ms
    exit_fee_usdt = _float(best.get("execFee"))
    backfill: dict[str, Any] = {
        "exit_price": exit_price,
        "exit_fee_usdt": exit_fee_usdt,
        # Bybit's createdTime IS the venue execution time for the close.
        "exit_exec_time_ms": closed_at_ms,
        "gross_trade_return": gross_trade_return,
        "net_return": gross_trade_return * notional_weight,
        "exit_ts_ms": closed_at_ms,
        "exit_trigger_ts_ms": closed_at_ms,
        "closed_at_ms": closed_at_ms,
        "submit_mode": "orphan_reconciled",
    }
    exit_order_id = str(best.get("orderId") or "")
    if exit_order_id:
        backfill["exit_order_id"] = exit_order_id
    return backfill

def _risk_order_row(
    *,
    link: str,
    ts_ms: int,
    symbol: str,
    side: str,
    qty: str,
    order_type: str,
    submit_mode: str,
    status: str,
    order_id: str = "",
    price: float = 0.0,
    time_in_force: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "order_link_id": link,
        "ts_ms": ts_ms,
        "trade_id": "",
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "qty": qty,
        "reduce_only": True,
        "order_id": order_id,
        "submit_mode": submit_mode,
        "avg_price": 0.0,
        "notional_usdt": 0.0,
        "status": status,
        "exit_reason": "",
        "price": price,
        "time_in_force": time_in_force,
        "error": error,
    }

def _limit_chase_price(*, bybit_side: str, reference_price: float, bps: float, tick_size: float) -> float:
    if reference_price <= 0.0:
        return 0.0
    if bybit_side == "Buy":
        raw = reference_price * (1.0 + bps / 10_000.0)
        return _round_price(raw, tick_size=tick_size or _fallback_tick_size(reference_price), rounding=ROUND_CEILING)
    raw = reference_price * (1.0 - bps / 10_000.0)
    return _round_price(raw, tick_size=tick_size or _fallback_tick_size(reference_price), rounding=ROUND_FLOOR)
