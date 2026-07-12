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
from ._common import MS_PER_HOUR, exact_duration_ms
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
    _first_float,
    _first_non_empty,
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
    _ratio_or_zero,
    _split_order_link_id,
    _split_qty_for_max_order_size,
    _stop_price_for_entry,
    _take_profit_price_for_entry,
    _trade_return,
    _wait_for_execution_summary,
)
from .order_execution import (
    order_fill_status,
    order_fully_filled,
    remaining_qty_within_tolerance,
)

_logger = logging.getLogger(__name__)

# Bybit's closed-PnL endpoint enforces a <=7-day startTime->endTime span, so a batched
# account-wide fetch must be PAGED in <=7-day windows (reconcile-core-4 re-audit F1).
CLOSED_PNL_MAX_WINDOW_MS = exact_duration_ms(days=7)


def _terminalize_stale_pending_entry_orders(
    orders: pl.DataFrame,
    *,
    live_position_symbols: set[str],
    live_open_entry_order_symbols: set[str],
    now_ms: int,
    live_position_legs: set[tuple[str, str]] | None = None,
    live_open_entry_order_legs: set[tuple[str, str]] | None = None,
    live_open_entry_order_links: set[str] | None = None,
    live_position_unknown_side_symbols: set[str] | None = None,
    live_open_entry_order_unknown_side_symbols: set[str] | None = None,
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
        entry_side = _normalized_position_side(order.get("trade_side") or order.get("side"))
        entry_leg = (symbol, entry_side)
        if live_position_legs is None:
            matching_live_position = symbol in live_position_symbols
        else:
            matching_live_position = (
                entry_leg in live_position_legs
                or (symbol, "") in live_position_legs
                or symbol in (live_position_unknown_side_symbols or set())
            )
        if live_open_entry_order_legs is None and live_open_entry_order_links is None:
            matching_live_order = symbol in live_open_entry_order_symbols
        else:
            matching_live_order = (
                link in (live_open_entry_order_links or set())
                or entry_leg in (live_open_entry_order_legs or set())
                or (symbol, "") in (live_open_entry_order_legs or set())
                or symbol in (live_open_entry_order_unknown_side_symbols or set())
            )
        if matching_live_position or matching_live_order:
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
        # Qty that actually carried a price, so the volume-weighted exit_price is
        # averaged over PRICED legs only. A leg that filled (qty>0) but resolved to a
        # zero avg_price (degraded venue summary + a 0 planned_exit fallback) used to
        # inflate the denominator without contributing to the numerator, dragging
        # exit_price toward zero on a split close (audit-iter1 event-demo-3).
        priced_filled_qty = 0.0
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
                            target_qty=sub_target,  # EXEC-6: aggregate all WS legs before deciding filled/partial
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
            if demo.submit_orders and sub_status not in {"failed", "submitted_unconfirmed"}:
                sub_status = order_fill_status(target_qty=sub_target, filled_qty=sub_filled_qty)
                if sub_status != "filled":
                    any_submitted_unconfirmed = True
            total_filled_qty += sub_filled_qty
            if sub_filled_qty > 0.0 and sub_avg_price > 0.0:
                total_fill_value += sub_avg_price * sub_filled_qty
                priced_filled_qty += sub_filled_qty
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
        # Average over the PRICED qty (not total_filled_qty) so an unpriced leg can't
        # drag exit_price toward zero (audit-iter1 event-demo-3). total_filled_qty
        # stays the denominator for the fully_filled check below.
        exit_price = (
            (total_fill_value / priced_filled_qty)
            if priced_filled_qty > 0.0 and total_fill_value > 0.0
            else _float(exit_plan.get("planned_exit_price"))
        )
        fully_filled = not demo.submit_orders or order_fully_filled(
            target_qty=target_qty,
            filled_qty=total_filled_qty,
        )
        entry_price = _float(trade.get("entry_price"))
        gross_trade_return = _trade_return(entry_price, exit_price, side=side)
        notional_weight = _ratio_or_zero(trade.get("notional_usdt"), trade.get("equity_usdt"))
        if fully_filled and exit_price > 0.0:
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
        elif fully_filled:
            # No resolvable exit price (exit_price <= 0) — e.g. a fully-delisted
            # coin gone from BOTH the universe and get_tickers, so the paper/dry-run
            # path has no `planned_exit_price`. Closing now would book a fabricated
            # exit_price=0 / 0% return into the ledger (BUG-5). Keep the trade OPEN
            # and retry next cycle when a price reappears; on the submit path the
            # venue settles it and `_reconcile_open_trades` orphan-closes it with the
            # real `get_closed_pnl` price. Never fall back to entry_price — that
            # books a still-fictional flat return.
            _logger.warning(
                "exit for %s (%s) skipped: no resolvable exit price "
                "(delisted / no ticker / no fill price); keeping trade open for retry",
                symbol,
                str(exit_plan.get("exit_reason") or ""),
            )
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
    live_position_by_symbol: dict[str, dict[str, Any]] | None = None,
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
            normalized_trade_side = _normalized_position_side(trade_side)
            current_position = (
                (live_position_by_symbol or {}).get(symbol, {})
                if live_position_by_symbol is not None
                else {}
            )
            current_position_side = _normalized_position_side(current_position.get("side"))
            current_position_size = _float(current_position.get("size"))
            # Execution history is still recovered for the historical ledger,
            # even after a one-way account has flipped sides. But trading-stop
            # mutates the CURRENT net venue position, so old short protection
            # must never be applied to a replacement long (or vice versa).
            protection_matches_live_leg = (
                live_position_by_symbol is None
                or (
                    current_position_size > 0.0
                    and bool(normalized_trade_side)
                    and current_position_side == normalized_trade_side
                )
            )
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
            protection_changed = (stop_loss_pct > 0.0 or take_profit_pct > 0.0) and (
                not _prices_close(entry_stop_price, recalculated_stop_price, tolerance_bps=0.0)
                or (
                    recalculated_take_profit_price > 0.0
                    and not _prices_close(entry_take_profit_price, recalculated_take_profit_price, tolerance_bps=0.0)
                )
            )
            if protection_changed and protection_matches_live_leg:
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
            elif protection_changed and live_position_by_symbol is not None:
                entry_stop_update_status = "skipped_opposite_or_absent_live_side"
                entry_stop_update_error = (
                    f"recovered {normalized_trade_side or 'unknown'} entry while live position side is "
                    f"{current_position_side or 'flat/unknown'}; venue protection not mutated"
                )[:500]
            entry_stop_price = recalculated_stop_price
            entry_take_profit_price = recalculated_take_profit_price
        order_update = dict(order)
        order_update.update(
            {
                "status": order_fill_status(
                    target_qty=target_qty,
                    filled_qty=filled_qty,
                    unfilled_status="partial",
                ),
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
            # Close only when the POSITION is gone. `fully_filled` is ORDER-level
            # fullness; a reduce order may target only part of the trade (e.g. a
            # rebalance_reduce), and closing on order fullness dropped the unfilled
            # remainder from the ledger while it stayed live on the venue
            # (audit 2026-06-12 round 3). `fully_filled` still drives the order
            # row's status above.
            if remaining_qty_within_tolerance(target_qty=trade.get("qty"), remaining_qty=remaining_qty):
                # gross_trade_return / net_return must land on the close so the
                # ledger carries realized PnL without depending on the orphan
                # reconciler. Both fields use the same formula as the cycle-exit
                # path (lines ~3525) and orphan backfill (lines ~4778).
                trade_side = str(trade.get("side") or "short")
                entry_price = _float(trade.get("entry_price"))
                gross_trade_return = _trade_return(entry_price, avg_price, side=trade_side)
                notional_weight = _ratio_or_zero(trade.get("notional_usdt"), trade.get("equity_usdt"))
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
            # ADD this order's NEW fill (delta since last reconcile) to the open
            # trade, never overwrite-when-greater. A cap-binding entry splits into
            # sub-orders that share a trade_id; if a non-first sub is recovered
            # here, its per-link `filled_qty` (e.g. 18750) is NOT greater than the
            # trade qty already carrying the first sub (18750), so the old
            # `filled_qty > existing.qty` gate dropped the second leg and the
            # ledger under-reported the position (BUG-3). Delta-add (mirroring the
            # reduce_only path) sums the legs and stays idempotent across cycles
            # because `filled_qty` is written back onto the order row each pass.
            delta_qty = max(filled_qty - previous_filled_qty, 0.0)
            # Adoption double-book guard (round 4): if ws_risk ADOPTED this trade
            # from the venue POSITION (submit_mode adopted_*), the adopted qty
            # already INCLUDES this order's fill — the position is the sum of its
            # fills. On the first reconcile pass after adoption the order row
            # still carries filled_qty="" (previous=0), so the delta-add would
            # add the full fill a second time onto the adopted row. Mark the
            # order reconciled (the order_update above records its cumulative
            # fill) but skip the trade-qty add; later passes delta-add normally.
            adoption_covers_fill = (
                previous_filled_qty <= 0.0
                and str(existing_trade.get("submit_mode") or "").startswith("adopted")
            )
            if str(existing_trade.get("status")) != "closed" and delta_qty > 0.0 and not adoption_covers_fill:
                prior_qty = _float(existing_trade.get("qty"))
                prior_entry = _float(existing_trade.get("entry_price"))
                # This ORDER contributed `previous_filled_qty` to the trade last
                # reconcile; its cumulative venue fill is now `filled_qty` at the
                # venue's cumulative `avg_price`. REPLACE this order's prior
                # contribution with its full current one while preserving every
                # OTHER order's contribution. This is correct for BOTH a single
                # order whose fill grew (prior_qty == previous_filled_qty, so the
                # other-value term is 0 and entry == the cumulative avg_price) AND
                # a split where a non-first sub is recovered (previous_filled_qty
                # == 0, so the first sub's value is kept and the legs SUM).
                other_qty = max(prior_qty - previous_filled_qty, 0.0)
                new_qty = other_qty + filled_qty
                order_price = avg_price if avg_price > 0.0 else prior_entry
                if new_qty > 0.0 and prior_entry > 0.0 and order_price > 0.0:
                    new_entry = (prior_entry * other_qty + order_price * filled_qty) / new_qty
                else:
                    new_entry = order_price if order_price > 0.0 else prior_entry
                leverage = _float(existing_trade.get("entry_leverage")) or _float(order.get("entry_leverage")) or demo.entry_leverage
                notional = abs(new_entry * new_qty) if new_entry > 0.0 else _float(existing_trade.get("notional_usdt"))
                initial_margin = notional / leverage if leverage > 0.0 else 0.0
                equity = _float(existing_trade.get("equity_usdt"))
                existing_trade.update(
                    {
                        "entry_price": new_entry,
                        "qty": _decimal_text(Decimal(str(new_qty))),
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
                # Identity fields ride the ORDER row for exactly this recovery
                # path. Without them the recovered trade had no sleeve tag —
                # _sleeve_of() defaults empty to "short", so a recovered
                # CONTINUOUS entry was mis-filed into the compatibility ledger
                # and became invisible to the continuous cycle's exits
                # and rebalance; a component row without component_weight would
                # additionally be resized to full base notional (round 4).
                "sleeve": str(order.get("sleeve") or ""),
                "strategy_id": str(order.get("strategy_id") or ""),
                "component": str(order.get("component") or ""),
                "component_weight": _float(order.get("component_weight")),
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
                # Order-producing sleeves stamp their own lifecycle deadline on
                # the durable intent row. Preserve it when ws_risk reconstructs
                # a fill after the producer crashed; otherwise the independent
                # max-hold authority sees an immortal recovered position.
                "planned_exit_ts_ms": int(order.get("planned_exit_ts_ms") or 0),
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
            tagged["order_link_attempt"] = int(_exit_plan.get("order_link_attempt") or 0)
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
                link_attempt=int(exit_plan.get("order_link_attempt") or 0),
                record_preflight=_record_with_context if record_preflight is not None else None,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in order telemetry so the loop can continue
            link = _risk_order_link_id(
                "rx",
                symbol=symbol,
                ts_ms=now_ms,
                attempt=int(exit_plan.get("order_link_attempt") or 0),
            )
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
                    "order_link_attempt": int(exit_plan.get("order_link_attempt") or 0),
                }
            )
            orders.append(failed_order)
            continue
        exit_price = _float(submit["exec_summary"].get("avg_price")) or planned_price
        target_qty = _float(qty)
        filled_qty = _float(submit["exec_summary"].get("qty"))
        exit_fee_usdt = _float(submit["exec_summary"].get("fee"))
        exit_exec_time_ms = int(_float(submit["exec_summary"].get("exec_time_ms") or 0))
        fully_filled = not risk.submit_orders or order_fully_filled(
            target_qty=target_qty,
            filled_qty=filled_qty,
        )
        for order_row in submit["order_rows"]:
            row_target_qty = str(order_row.get("target_qty") or order_row.get("qty") or qty)
            row_filled_qty = _float(order_row.get("filled_qty"))
            row_status = str(order_row.get("status") or "")
            if risk.submit_orders and row_status in {"", "submitted"}:
                order_row["status"] = order_fill_status(
                    target_qty=row_target_qty,
                    filled_qty=row_filled_qty,
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
                    "order_link_attempt": int(exit_plan.get("order_link_attempt") or 0),
                }
            )
            orders.append(order_row)
        if fully_filled and exit_price > 0.0:
            # Mirror the cycle-exit and pending-exit-reconcile paths: a closed
            # trade must carry both gross_trade_return and net_return so the
            # orphan reconciler does not have to backfill them post-hoc.
            trade_side = str(trade.get("side") or "short")
            entry_price = _float(trade.get("entry_price"))
            gross_trade_return = _trade_return(entry_price, exit_price, side=trade_side)
            notional_weight = _ratio_or_zero(trade.get("notional_usdt"), trade.get("equity_usdt"))
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
        elif fully_filled:
            # event-demo-core-2: mirror the cycle path's BUG-5 guard. exit_price
            # <= 0.0 means no resolvable price (e.g. a max_hold exit on a
            # fully-delisted / no-ticker symbol in the dry-run path, where the
            # planned price falls back to 0.0). Booking a "closed" trade now would
            # record a fabricated exit_price=0 / 0% gross_trade_return into the
            # ledger — a fictional flat round-trip. Keep the trade OPEN and retry
            # next cycle when a price reappears; on the submit path the venue
            # settles it and the orphan reconciler books the real get_closed_pnl
            # price. Never fall back to entry_price (still a fictional flat
            # return).
            _logger.warning(
                "risk exit for %s (%s) skipped: no resolvable exit price "
                "(delisted / no ticker / no fill price); keeping trade open for retry",
                symbol,
                str(exit_plan.get("exit_reason") or ""),
            )
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
    link_attempt: int = 0,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not risk.submit_orders:
        link = _risk_order_link_id("rx", symbol=symbol, ts_ms=now_ms, attempt=link_attempt)
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
        base_link = _risk_order_link_id("rx", symbol=symbol, ts_ms=now_ms, attempt=link_attempt)
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
        priced_filled_qty = 0.0  # qty that carried a price (audit-iter1 event-demo-3)
        total_fee = 0.0
        max_exec_time_ms = 0
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
                sub_status = order_fill_status(target_qty=sub_target, filled_qty=sub_filled_qty)
                if sub_status != "filled":
                    any_submitted_unconfirmed = True
            sub_avg_price = _float(sub_exec_summary.get("avg_price"))
            # Fee + venue fill-time aggregation (audit 2026-06-09): the cycle-exit
            # split path and the limit-chase path both carry execFee / execTime
            # through to the trade row; this market split path dropped them
            # (hardcoded agg fee 0.0), so every ws_risk market exit recorded
            # exit_fee_usdt=0.0 and exit_exec_time_ms=0. Mirror the cycle path.
            sub_fee = _float(sub_exec_summary.get("fee"))
            sub_exec_time_ms = int(_float(sub_exec_summary.get("exec_time_ms") or 0))
            total_fee += sub_fee
            if sub_exec_time_ms > max_exec_time_ms:
                max_exec_time_ms = sub_exec_time_ms
            total_filled_qty += sub_filled_qty
            if sub_filled_qty > 0.0 and sub_avg_price > 0.0:
                total_fill_value += sub_avg_price * sub_filled_qty
                priced_filled_qty += sub_filled_qty
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
                    "fee_usdt": sub_fee,
                    "exec_time_ms": sub_exec_time_ms,
                    "notional_usdt": abs(sub_avg_price * sub_filled_qty) if sub_avg_price > 0.0 else 0.0,
                }
            )
            order_rows.append(sub_row)

        target_qty = _float(qty)
        # Average over PRICED qty so an unpriced leg can't bias avg_price low
        # (audit-iter1 event-demo-3).
        avg_price = (
            (total_fill_value / priced_filled_qty)
            if priced_filled_qty > 0.0 and total_fill_value > 0.0
            else 0.0
        )
        agg_summary: dict[str, Any] = {
            "qty": _decimal_text(Decimal(str(total_filled_qty))) if total_filled_qty > 0.0 else "",
            "avg_price": avg_price,
            "fee": total_fee,
            "exec_time_ms": max_exec_time_ms,
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
        link_attempt=link_attempt,
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
    link_attempt: int = 0,
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
        if remaining_qty_within_tolerance(target_qty=target_qty, remaining_qty=remaining_qty):
            break
        link = _risk_order_link_id(
            "lc", symbol=symbol, ts_ms=now_ms, attempt=link_attempt * 100 + attempt
        )
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
        row_status = order_fill_status(
            target_qty=remaining_qty,
            filled_qty=order_filled_qty,
            unfilled_status="unfilled",
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
    if (
        not remaining_qty_within_tolerance(target_qty=target_qty, remaining_qty=remaining_qty)
        and risk.limit_chase_fallback_market
    ):
        link = _risk_order_link_id(
            "lm", symbol=symbol, ts_ms=now_ms, attempt=link_attempt * 100 + attempts
        )
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
            status = order_fill_status(
                target_qty=remaining_qty,
                filled_qty=order_filled_qty,
                unfilled_status="fallback_market",
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
    require_evidence = getattr(demo, "orphan_close_require_evidence", True)
    updates: list[dict[str, Any]] = []
    kept = []
    trade_dicts = open_trades.to_dicts()
    # reconcile-core-4: one account-wide closed-PnL fetch for the whole pass instead
    # of one blocking REST call per orphan (see _fetch_account_closed_pnl). The orphan
    # predicate matches the loop's keep-open check below.
    cycle_orphans = [
        t for t in trade_dicts
        if not (
            _normalized_position_side(t.get("side"))
            and size_by_symbol_side.get((str(t["symbol"]), _normalized_position_side(t.get("side"))), 0.0) > 0.0
        )
    ]
    closed_pnl_by_symbol = _fetch_account_closed_pnl(trading_client, cycle_orphans, now_ms=now_ms)
    closed_pnl_by_symbol = _attach_closed_order_metadata(
        closed_pnl_by_symbol,
        _fetch_closed_order_metadata(
            trading_client,
            closed_pnl_by_symbol,
            cycle_orphans,
            now_ms=now_ms,
        ),
    )
    for trade in trade_dicts:
        symbol = str(trade["symbol"])
        # Normalize through the same helper so "short" / "Sell" both land
        # on "short" — trade rows carry "short"/"long" and Bybit positions
        # carry "Sell"/"Buy", but the lookup must agree.
        trade_side = _normalized_position_side(trade.get("side"))
        if trade_side and size_by_symbol_side.get((symbol, trade_side), 0.0) > 0.0:
            kept.append(trade)
            continue
        # A healthy batch provides a symbol slice; an absent/failed batch stays
        # pending under evidence mode. We deliberately do not fall back to an
        # unattributed per-symbol feed on the shared account.
        source_records = closed_pnl_by_symbol.get(symbol, []) if closed_pnl_by_symbol is not None else None
        if require_evidence:
            # A REST position omission plus an account-wide same-symbol close is
            # ambiguous on the shared netted account, even when quantity matches.
            # The cycle path has no private-WS flat event, so only a venue order ID
            # already recorded on this exact trade is attributable enough. A
            # failed account-wide fetch stays pending; falling back to an
            # unfiltered per-symbol fetch would reopen the same attribution bug.
            exit_order_id = str(trade.get("exit_order_id") or "")
            symbol_records = [
                record
                for record in (source_records or [])
                if exit_order_id and str(record.get("orderId") or "") == exit_order_id
            ]
        else:
            symbol_records = source_records
        row = _orphan_close_trade_row(
            trade, now_ms=now_ms, trading_client=trading_client, require_evidence=require_evidence,
            closed_pnl_records=symbol_records,
        )
        if row is None:
            # FAIL-CLOSED: position absent but no closure evidence — keep the
            # trade OPEN rather than wipe a possibly-live position. Surfaced so
            # a persistently-unconfirmed trade is visible to the operator.
            kept.append(trade)
            _logger.warning(
                "orphan-close skipped (no closure evidence) symbol=%s side=%s trade_id=%s — keeping open",
                symbol, trade_side, trade.get("trade_id"),
            )
            continue
        updates.append(row)
    return pl.DataFrame(kept, infer_schema_length=None) if kept else _empty_trades(), updates, ""

def _risk_reconcile_missing_positions(
    open_trades: pl.DataFrame,
    *,
    position_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    enabled: bool,
    position_error: str = "",
    trading_client: Any | None = None,
    require_evidence: bool = False,
    confirmed_flat_symbols: set[str] | None = None,
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Close ledger rows whose Bybit position has vanished.

    Skipped when ``position_error`` is set: a failed ``get_positions`` returns an
    empty ``position_by_symbol`` that is indistinguishable from "no positions",
    so without this guard every open trade would be false-positive orphan-closed
    on a single transient API failure. The caller plumbs the error string from
    :func:`_safe_raw_positions`.

    ``require_evidence`` extends that protection to the *successful-but-empty*
    snapshot (retCode 0 with an empty ``result.list`` during a venue degradation),
    which carries no ``position_error`` yet is the same C1 mass-false-close class.
    With it set, a trade is closed ONLY when ``get_closed_pnl`` confirms a real
    close since entry; absent that record the trade is kept OPEN (a possibly-live
    position is never wiped to a zero-PnL ``bybit_position_missing`` row). The bulk
    REST-snapshot callers (``rest_reconcile`` / ``bootstrap`` / the cycle runner)
    pass ``True``. A private-WS ``size=0`` marks the symbol independently flat and
    suppresses futile reduce-only retries, but the ledger row still waits for
    attributable Closed-PnL legs covering its full quantity so price and fees remain
    reconstructable.

    When a ``trading_client`` is provided, account Closed-PnL is fetched in bounded
    batches and used to backfill ``exit_price`` / ``gross_trade_return`` /
    ``net_return`` / ``exit_order_id`` / ``exit_ts_ms`` from the actual close.
    """
    if open_trades.is_empty() or not enabled:
        return open_trades, []
    if position_error:
        return open_trades, []
    # Side-aware keep-open check, mirroring the cycle path (_reconcile_open_trades):
    # key on (symbol, normalized_side) so a same-symbol flip (ledger short while the
    # venue now holds a long on that symbol) is surfaced as a compatibility short orphan
    # close instead of being masked by the opposite-side position. position_by_symbol
    # values carry `side` in both the WS (on_position_message) and REST
    # (_active_position_by_symbol) population paths.
    size_by_symbol_side = _position_size_by_symbol_side(list(position_by_symbol.values()))
    updates: list[dict[str, Any]] = []
    kept = []
    trade_dicts = open_trades.to_dicts()
    # reconcile-core-4: pre-compute the orphans (ledger rows whose venue position has
    # vanished) so we can do ONE account-wide closed-PnL fetch instead of one blocking
    # REST call per orphan inside the loop -- the per-orphan calls stalled the
    # single-threaded WS consumer on a synchronized multi-position close.
    orphans = []
    for trade in trade_dicts:
        symbol = str(trade.get("symbol", ""))
        trade_side = _normalized_position_side(trade.get("side"))
        if trade_side:
            keep_open = size_by_symbol_side.get((symbol, trade_side), 0.0) > 0.0
        else:
            keep_open = bool(symbol) and symbol in position_by_symbol
        if not keep_open:
            orphans.append(trade)
    closed_pnl_by_symbol = _fetch_account_closed_pnl(trading_client, orphans, now_ms=now_ms)
    closed_pnl_by_symbol = _attach_closed_order_metadata(
        closed_pnl_by_symbol,
        _fetch_closed_order_metadata(
            trading_client,
            closed_pnl_by_symbol,
            orphans,
            now_ms=now_ms,
        ),
    )
    confirmed_flat_symbols = confirmed_flat_symbols or set()
    orphan_group_counts: dict[tuple[str, str], int] = {}
    for trade in orphans:
        key = (str(trade.get("symbol") or ""), _normalized_position_side(trade.get("side")))
        orphan_group_counts[key] = orphan_group_counts.get(key, 0) + 1
    allocated_records = (
        _allocate_group_closed_pnl_records(orphans, closed_pnl_by_symbol)
        if closed_pnl_by_symbol is not None
        else {}
    )
    for trade in trade_dicts:
        symbol = str(trade.get("symbol", ""))
        trade_side = _normalized_position_side(trade.get("side"))
        if trade_side:
            keep_open = size_by_symbol_side.get((symbol, trade_side), 0.0) > 0.0
        else:
            # Degenerate row with no parseable side (malformed / schema-drift): fall
            # back to symbol-only presence so a possibly-live position we simply
            # can't side-match is never spuriously orphan-closed (re-audit
            # rescan-reconcile-2). Normal rows always carry "short"/"long".
            keep_open = bool(symbol) and symbol in position_by_symbol
        if keep_open:
            kept.append(trade)
            continue
        # reconcile-core-4: a symbol absent from a SUCCESSFUL account-wide fetch means
        # "no closure for it yet" -> pass [] (the matcher returns {} -> require_evidence
        # keeps it open), NOT None. None is reserved for a FAILED/absent batch, which
        # alone falls back to the legacy per-symbol fetch. Using .get(symbol) without the
        # [] default would re-fire a per-orphan REST call for every not-yet-closed orphan
        # — exactly the synchronized-mass-close stall this fix removes.
        group_key = (symbol, trade_side)
        if require_evidence:
            if symbol in confirmed_flat_symbols:
                if orphan_group_counts.get(group_key, 0) > 1:
                    # A private-WS size=0 event proves the net position is gone.
                    # Allocate account-level closedSize/fee across component rows
                    # once; if the aggregate record qty is insufficient, keep the
                    # whole group open instead of reusing one partial close N times.
                    symbol_records = allocated_records.get(str(trade.get("trade_id") or ""), [])
                else:
                    symbol_records = (
                        closed_pnl_by_symbol.get(symbol, [])
                        if closed_pnl_by_symbol is not None
                        else None
                    )
            else:
                # A REST-absent netted position can be false-empty. Account-wide
                # same-symbol Closed-PnL is also not sleeve-attributable, even for
                # a single row. Only an order ID already recorded on this exact
                # trade is uniquely attributable enough to close it.
                exit_order_id = str(trade.get("exit_order_id") or "")
                source_records = closed_pnl_by_symbol.get(symbol, []) if closed_pnl_by_symbol is not None else []
                symbol_records = [
                    record for record in source_records
                    if exit_order_id and str(record.get("orderId") or "") == exit_order_id
                ]
        else:
            symbol_records = closed_pnl_by_symbol.get(symbol, []) if closed_pnl_by_symbol is not None else None
        row = _orphan_close_trade_row(
            trade, now_ms=now_ms, trading_client=trading_client, require_evidence=require_evidence,
            closed_pnl_records=symbol_records,
        )
        if row is None:
            # FAIL-CLOSED: position absent but no closure evidence — keep the
            # trade OPEN rather than wipe a possibly-live position on a degraded/
            # empty REST snapshot. untracked_position_grace_seconds gates the
            # OPPOSITE direction (adopting an exchange position with no ledger
            # row), not this ledger-row-with-no-position close.
            kept.append(trade)
            _logger.warning(
                "risk orphan-close skipped (no closure evidence) symbol=%s trade_id=%s — keeping open",
                symbol, trade.get("trade_id"),
            )
            continue
        updates.append(row)
    return pl.DataFrame(kept, infer_schema_length=None) if kept else _empty_trades(), updates


def _allocate_group_closed_pnl_records(
    orphans: list[dict[str, Any]],
    records_by_symbol: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Allocate account-level close legs across same-symbol ledger components.

    One netted reduce-only order can legitimately close several component rows,
    so a record may be split by ``closedSize``. Its fee is prorated with the
    allocated quantity. Allocation is all-or-nothing per symbol+side group: a
    partial/sibling close can never be reused to evidence-close every row.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for trade in orphans:
        symbol = str(trade.get("symbol") or "")
        side = _normalized_position_side(trade.get("side"))
        groups.setdefault((symbol, side), []).append(trade)
    out: dict[str, list[dict[str, Any]]] = {}
    for (symbol, side), trades in groups.items():
        if len(trades) <= 1:
            continue
        expected_close_side = "Buy" if side == "short" else "Sell"
        pool: list[dict[str, Any]] = []
        for record in records_by_symbol.get(symbol, []):
            size = max(_float(record.get("closedSize")), 0.0)
            if str(record.get("side") or "") != expected_close_side or size <= 0.0:
                continue
            pool.append({
                "record": record,
                "remaining": size,
                "original_size": size,
                "created_ts": int(_float(record.get("createdTime") or record.get("updatedTime") or 0)),
            })
        pool.sort(key=lambda item: int(item["created_ts"]), reverse=True)
        group_allocations: dict[str, list[dict[str, Any]]] = {}
        complete = True
        # Latest-open trades receive latest eligible closes first. This preserves
        # the entry-time lower bound while keeping each source record's quantity
        # and fee single-use across the group.
        for trade in sorted(
            trades,
            key=lambda row: (int(row.get("entry_ts_ms") or 0), str(row.get("trade_id") or "")),
            reverse=True,
        ):
            trade_id = str(trade.get("trade_id") or "")
            need = max(_float(trade.get("qty")), 0.0)
            if not trade_id or need <= 0.0:
                complete = False
                break
            entry_ts_ms = int(trade.get("entry_ts_ms") or 0)
            allocated: list[dict[str, Any]] = []
            tolerance = max(need * 1e-8, 1e-12)
            for item in pool:
                if need <= tolerance:
                    break
                if float(item["remaining"]) <= 0.0:
                    continue
                created_ts = int(item["created_ts"])
                if entry_ts_ms > 0 and created_ts > 0 and created_ts < entry_ts_ms:
                    continue
                take = min(need, float(item["remaining"]))
                source = dict(item["record"])
                source["closedSize"] = _quantity_text(take)
                original_size = float(item["original_size"])
                for value_key in ("openFee", "closeFee", "closedPnl"):
                    if source.get(value_key) not in (None, ""):
                        source[value_key] = _float(source.get(value_key)) * take / original_size
                if source.get("closeFee") in (None, "") and source.get("execFee") not in (None, ""):
                    source["execFee"] = _float(source.get("execFee")) * take / original_size
                allocated.append(source)
                item["remaining"] = float(item["remaining"]) - take
                need -= take
            if need > tolerance:
                complete = False
                break
            group_allocations[trade_id] = allocated
        if complete and len(group_allocations) == len(trades):
            out.update(group_allocations)
    return out

def _orphan_close_trade_row(
    trade: dict[str, Any],
    *,
    now_ms: int,
    trading_client: Any | None,
    require_evidence: bool = True,
    closed_pnl_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build an orphan-close trade row, backfilling PnL from Bybit when possible.

    FAIL-CLOSED invariant: when ``require_evidence`` (the default), the close is
    produced ONLY if ``_orphan_close_pnl_backfill`` finds POSITIVE evidence of
    closure (a closed-PnL record since entry). With no evidence this returns
    ``None`` so the caller keeps the trade OPEN — a transient/empty positions
    read must never wipe a live position from the ledger (the C1 class). Set
    ``require_evidence=False`` to restore the legacy close-on-absence behavior
    (zero-PnL close when no record is found).
    """
    backfill = _orphan_close_pnl_backfill(
        trade,
        now_ms=now_ms,
        trading_client=trading_client,
        closed_pnl_records=closed_pnl_records,
        require_quantity_coverage=require_evidence,
    )
    if require_evidence and not backfill:
        return None
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
    if backfill:
        updated.update(backfill)
        close_reason, reason_source = _classify_orphan_close_reason(trade, backfill)
        if close_reason:
            updated["exit_reason"] = close_reason
            updated["exit_reason_source"] = reason_source
    return updated

def _fetch_account_closed_pnl(
    trading_client: Any | None,
    orphans: list[dict[str, Any]],
    *,
    now_ms: int,
) -> dict[str, list[dict[str, Any]]] | None:
    """reconcile-core-4: batched closed-PnL fetch for a whole reconcile pass, grouped
    by symbol, replacing the per-orphan ``get_closed_pnl(symbol=...)`` round-trips that
    stalled the single-threaded WS consumer on a synchronized multi-position close
    (N blocking REST calls inline -> O(span/7d)).

    Covers each orphan's full ``[entry_ts_ms - 1h, now]`` window. Bybit's closed-PnL
    endpoint enforces a <=7-day startTime->endTime span (re-audit F1), so a SINGLE call
    from the OLDEST orphan's entry to now would silently drop a RECENT orphan's closure
    when an old orphan (the long sleeve holds up to ~21 days) shares the pass -- wrongly
    keeping the recent orphan OPEN under require_evidence. We therefore PAGE the
    account-wide fetch in disjoint <=7-day windows from the oldest entry to ``now``:
    O(ceil(span/7d)) calls (~3 for a 21-day horizon), still independent of the orphan
    COUNT. Records are de-duplicated by orderId so a boundary record returned by two
    adjacent windows is not double-counted as a phantom multi-leg. The authoritative
    per-trade filters (``created_ts >= entry_ts_ms`` and close side) are re-applied
    in-memory by ``_orphan_close_pnl_from_records``, so the paged union yields IDENTICAL
    matches to N per-symbol windows.

    Returns ``None`` (NOT ``{}``) when the endpoint is absent, the call raises, or any
    orphan has a non-positive ``entry_ts_ms`` (no reliable window bound) -- the caller
    then degrades to the legacy per-orphan fetch, never to a close-on-absence. An empty
    account ({} = no closures) is a real, non-None result and correctly leaves
    require_evidence orphans OPEN.
    """
    if trading_client is None or not orphans:
        return None
    get_closed_pnl = getattr(trading_client, "get_closed_pnl", None)
    if not callable(get_closed_pnl):
        return None
    entry_times = [int(t.get("entry_ts_ms") or 0) for t in orphans]
    if any(ts <= 0 for ts in entry_times):
        # An unbounded-window orphan (malformed / adopted row) can't be safely covered
        # by a paged window; fall back to the legacy per-orphan fetch for the whole pass.
        return None
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    window_start = max(min(entry_times) - MS_PER_HOUR, 0)
    try:
        while window_start <= now_ms:
            # Do NOT cap window_end at now_ms (re-audit F1 Concern A): the legacy per-symbol
            # fetch has no upper bound, so a close stamped slightly AFTER now_ms (venue clock
            # ahead / a stale now_ms) must still be captured. The last window runs the full
            # 7d-1 span (which always reaches >= now_ms), and the matcher applies no upper
            # time bound either -> matches legacy. Each window is still < 7d (within the clamp).
            window_end = window_start + CLOSED_PNL_MAX_WINDOW_MS - 1
            records = get_closed_pnl(
                symbol=None, start_time_ms=window_start, end_time_ms=window_end, limit=100,
            )
            for record in records or []:
                sym = str(record.get("symbol") or "")
                if not sym:
                    continue
                # Dedup ONLY by a real orderId (re-audit F1 Concern B): windows are disjoint
                # so overlap can't double-count, but a boundary record returned by Bybit in
                # two windows shares its orderId. NEVER dedup an orderId-less record by a
                # content tuple — two DISTINCT legs can share (symbol,createdTime,closedSize,
                # avgExitPrice) yet differ in fee, and merging them would undercount the close.
                oid = str(record.get("orderId") or "")
                if oid:
                    if oid in seen:
                        continue
                    seen.add(oid)
                by_symbol.setdefault(sym, []).append(record)
            window_start = window_end + 1
    except Exception:  # noqa: BLE001 - degrade to per-orphan fetch; never crash reconcile
        return None
    return by_symbol


def _fetch_closed_order_metadata(
    trading_client: Any | None,
    records_by_symbol: dict[str, list[dict[str, Any]]] | None,
    orphans: list[dict[str, Any]],
    *,
    now_ms: int,
) -> dict[str, dict[str, Any]]:
    """Fetch Bybit's actual close-order type for orphan Closed-PnL rows.

    Closed-PnL proves price, quantity, fees, and account P&L, but it does not
    say whether the venue order was TP, SL, liquidation, or a manual close.
    Order history does. Fetch it account-wide in the same bounded seven-day
    windows used for Closed-PnL so a synchronized multi-position close remains
    O(time windows), never one blocking REST request per ledger component.
    """
    if trading_client is None or not records_by_symbol or not orphans:
        return {}
    get_order_history = getattr(trading_client, "get_order_history", None)
    if not callable(get_order_history):
        return {}

    expected_close_legs = {
        (
            str(trade.get("symbol") or ""),
            "Buy" if _normalized_position_side(trade.get("side")) == "short" else "Sell",
        )
        for trade in orphans
        if str(trade.get("symbol") or "")
        and _normalized_position_side(trade.get("side")) in {"short", "long"}
    }
    target_ids: set[str] = set()
    target_times: list[int] = []
    for symbol, records in records_by_symbol.items():
        for record in records:
            if (symbol, str(record.get("side") or "")) not in expected_close_legs:
                continue
            order_id = str(record.get("orderId") or "")
            if not order_id:
                continue
            target_ids.add(order_id)
            created_ms = int(_float(record.get("createdTime") or record.get("updatedTime") or 0))
            if created_ms > 0:
                target_times.append(created_ms)
    if not target_ids or not target_times:
        return {}

    window_start = max(min(target_times) - MS_PER_HOUR, 0)
    last_target_ms = max(max(target_times), int(now_ms))
    metadata: dict[str, dict[str, Any]] = {}
    try:
        while window_start <= last_target_ms and target_ids - metadata.keys():
            window_end = min(
                window_start + CLOSED_PNL_MAX_WINDOW_MS - 1,
                last_target_ms,
            )
            history = get_order_history(
                settle_coin="USDT",
                start_time_ms=window_start,
                end_time_ms=window_end,
                limit=50,
                max_pages=100,
            )
            for row in history or []:
                order_id = str(row.get("orderId") or row.get("order_id") or "")
                if order_id not in target_ids:
                    continue
                candidate = {
                    "stop_order_type": str(
                        row.get("stopOrderType") or row.get("stop_order_type") or ""
                    ),
                    "create_type": str(row.get("createType") or row.get("create_type") or ""),
                    "order_link_id": str(
                        row.get("orderLinkId") or row.get("order_link_id") or ""
                    ),
                    "order_status": str(row.get("orderStatus") or row.get("order_status") or ""),
                }
                previous = metadata.get(order_id, {})
                previous_score = sum(bool(previous.get(key)) for key in previous)
                candidate_score = sum(bool(candidate.get(key)) for key in candidate)
                if candidate_score >= previous_score:
                    metadata[order_id] = candidate
            window_start = window_end + 1
    except Exception as exc:  # noqa: BLE001 - close still has Closed-PnL evidence
        _logger.warning(
            "close-order metadata unavailable; venue close cause will remain conservative: %s",
            exc,
        )
        return {}
    return metadata


def _attach_closed_order_metadata(
    records_by_symbol: dict[str, list[dict[str, Any]]] | None,
    metadata_by_order_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]] | None:
    if records_by_symbol is None or not metadata_by_order_id:
        return records_by_symbol
    output: dict[str, list[dict[str, Any]]] = {}
    for symbol, records in records_by_symbol.items():
        output[symbol] = []
        for record in records:
            enriched = dict(record)
            metadata = metadata_by_order_id.get(str(record.get("orderId") or ""), {})
            for key, value in metadata.items():
                if value:
                    enriched[f"_venue_{key}"] = value
            output[symbol].append(enriched)
    return output


def _classify_orphan_close_reason(
    trade: dict[str, Any],
    backfill: dict[str, Any],
) -> tuple[str, str]:
    """Return a truthful close label plus the evidence that supports it."""
    metadata_causes: set[str] = set()
    stop_types = str(backfill.get("venue_stop_order_type") or "").split(",")
    create_types = str(backfill.get("venue_create_type") or "").split(",")
    for raw in stop_types + create_types:
        normalized = "".join(character for character in raw.lower() if character.isalnum())
        if not normalized or normalized in {"unknown", "stop"}:
            continue
        if "partialtakeprofit" in normalized or "takeprofit" in normalized:
            metadata_causes.add("take_profit")
        elif "partialstoploss" in normalized or "stoploss" in normalized:
            metadata_causes.add("stop_loss")
        elif "trailingprofit" in normalized:
            metadata_causes.add("trailing_take_profit")
        elif "trailingstop" in normalized:
            metadata_causes.add("trailing_stop")
        elif "takeover" in normalized or normalized.endswith("liq"):
            metadata_causes.add("liquidation")
        elif "adl" in normalized:
            metadata_causes.add("auto_deleveraging")
        elif "settle" in normalized or "delivery" in normalized:
            metadata_causes.add("settlement")
        elif "createbyclosing" in normalized or "mmrateclose" in normalized:
            metadata_causes.add("manual_close")
    if len(metadata_causes) == 1:
        return next(iter(metadata_causes)), "bybit_order_history"
    if len(metadata_causes) > 1:
        return "mixed_venue_close", "bybit_order_history_mixed"

    exit_price = _float(backfill.get("exit_price"))
    take_profit = _float(trade.get("take_profit_price") or trade.get("takeProfit"))
    stop = _float(trade.get("stop_price") or trade.get("stopLoss"))
    side = _normalized_position_side(trade.get("side"))
    if exit_price > 0.0 and take_profit > 0.0 and (
        (side == "short" and exit_price <= take_profit)
        or (side == "long" and exit_price >= take_profit)
    ):
        return "take_profit_level_reached", "exit_price_vs_ledger_take_profit"
    if exit_price > 0.0 and stop > 0.0 and (
        (side == "short" and exit_price >= stop)
        or (side == "long" and exit_price <= stop)
    ):
        return "stop_loss_level_reached", "exit_price_vs_ledger_stop"
    return "", ""


def _orphan_close_pnl_backfill(
    trade: dict[str, Any],
    *,
    now_ms: int,
    trading_client: Any | None,
    closed_pnl_records: list[dict[str, Any]] | None = None,
    require_quantity_coverage: bool = False,
) -> dict[str, Any]:
    """Query Bybit closed-PnL for an orphan trade and return backfill fields.

    Returns an empty dict on any failure -- the caller keeps the zero-PnL defaults.

    reconcile-core-4: when ``closed_pnl_records`` is provided (the symbol's slice of
    a single account-wide fetch done once per reconcile pass), NO per-orphan REST
    call is made -- the matcher runs against the supplied in-memory records. When
    ``None`` the legacy per-symbol ``get_closed_pnl`` fetch runs, so every
    pre-existing single-call caller is byte-for-byte unchanged.
    """
    if closed_pnl_records is not None:
        return _orphan_close_pnl_from_records(
            trade,
            now_ms=now_ms,
            records=closed_pnl_records,
            require_quantity_coverage=require_quantity_coverage,
        )
    if trading_client is None:
        return {}
    symbol = str(trade.get("symbol", ""))
    if not symbol:
        return {}
    entry_ts_ms = int(trade.get("entry_ts_ms") or 0)
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
    return _orphan_close_pnl_from_records(
        trade,
        now_ms=now_ms,
        records=records,
        require_quantity_coverage=require_quantity_coverage,
    )


def _orphan_close_pnl_from_records(
    trade: dict[str, Any],
    *,
    now_ms: int,
    records: list[dict[str, Any]] | None,
    require_quantity_coverage: bool = False,
) -> dict[str, Any]:
    """Pure matcher: given already-fetched closed-PnL ``records`` for this trade's
    symbol, return the backfill fields (or {} when no record evidences a close since
    entry). Split out of ``_orphan_close_pnl_backfill`` (reconcile-core-4) so the
    account-wide batched fetch and the legacy per-symbol fetch share ONE matching
    path -- the body below is unchanged, so results stay numerically identical (no
    I/O here, fully deterministic).
    """
    if not records:
        return {}
    side = str(trade.get("side") or "short")
    entry_ts_ms = int(trade.get("entry_ts_ms") or 0)
    entry_price = _float(trade.get("entry_price"))
    # NOTE: a non-positive entry_price (a malformed / adopted-* row) does NOT
    # short-circuit here. Closure EVIDENCE (a closed-PnL record) is independent of
    # entry_price; only the return COMPUTATION below needs it. Bailing here would
    # let require_evidence=True keep an entry_price<=0 trade OPEN forever even
    # after a genuine venue close (re-audit rescan-reconcile-3).
    # Close side: for our short trade the closing order is Buy; for long it is Sell.
    expected_close_side = "Buy" if side == "short" else "Sell"
    candidates: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        record_side = str(record.get("side") or "")
        # Fail CLOSED on a missing/empty side. On the SHARED, NETTED demo account
        # get_closed_pnl is account-wide (symbol-scoped, but every sleeve can trade the
        # same symbol), and this matcher IS the positive close-evidence under
        # require_evidence=True. A blank-side record (venue anomaly) must NOT be
        # accepted as a close for THIS trade -- that would close a still-live position
        # and book a sibling sleeve's exit price. Mirrors reconciliation.py:957/967,
        # which already drops empty-side closed-PnL rows on the shared account.
        if record_side != expected_close_side:
            continue
        # Bybit returns createdTime / updatedTime as ms-since-epoch strings or ints.
        created_ts = int(_float(record.get("createdTime") or record.get("updatedTime") or 0))
        if entry_ts_ms > 0 and created_ts > 0 and created_ts < entry_ts_ms:
            continue
        candidates.append((created_ts, record))
    if not candidates:
        return {}
    # Multi-leg close: one ledger position can be closed by several reduce-only
    # orders (a partial-exit sequence, or a max-order-qty split), so closed_pnl
    # returns multiple rows. Pricing the close off a single most-recent leg
    # undercounts the exit fee and mis-prices a partial close. Aggregate ALL
    # matching legs since entry — sum execFee, qty-weight avgExitPrice by
    # closedSize, take the last leg's venue time/orderId. (M8) Single-leg
    # closures and rows that omit closedSize degrade to the leg's own price. (M8)
    candidates.sort(key=lambda item: item[0])  # ascending by createdTime
    legs = [record for _, record in candidates]
    # event-demo-core-3: on the SHARED netted demo account get_closed_pnl is
    # account-wide (symbol+time-window only, no per-sleeve key), so a SIBLING
    # sleeve's same-side close on this symbol since entry_ts_ms is also returned
    # here. Aggregating ALL such legs would fold the sibling's close into THIS
    # trade's exit price / fee / return. Cap the aggregation at this trade's own
    # ledgered qty: take legs earliest-first until cumulative closedSize covers
    # the trade qty (within tolerance), then stop — a sibling's surplus close
    # legs beyond our qty can no longer reprice this leg. A leg whose orderId
    # matches this trade's recorded exit_order_id is always kept (it is provably
    # ours). When the trade has no usable qty or no leg carries a usable
    # closedSize, fall back to the legacy all-legs behavior (no regression for
    # single-leg / size-less closures).
    trade_qty = _float(trade.get("qty"))
    trade_exit_order_id = str(trade.get("exit_order_id") or "")
    has_usable_size = any(_float(r.get("closedSize")) > 0.0 for r in legs)
    if trade_qty > 0.0 and has_usable_size and len(legs) > 1:
        qty_tolerance = max(trade_qty * 1e-8, 1e-12)
        # audit2c ([4]): selecting OUR close legs out of an account-wide closed-PnL feed
        # (a SIBLING sleeve may have closed the same side near our entry) is genuinely
        # ambiguous without per-sleeve keys — earliest-first mis-priced this trade off a
        # sibling's EARLIER close, latest-first off a sibling's LATER one. Disambiguate
        # by the strongest available signals, in order:
        #   1. provably-ours legs: those whose orderId matches the recorded exit_order_id
        #      (when we have one) — never a sibling's; use them alone.
        #   2. a single leg whose size matches our ledgered qty: almost certainly our
        #      whole close (prefer the LATEST such — an orphan is found when OUR position
        #      closed, i.e. the most recent same-size close).
        #   3. otherwise cover qty LATEST-first (our close completes most recently).
        ours = [leg for leg in legs if trade_exit_order_id and str(leg.get("orderId") or "") == trade_exit_order_id]
        if ours:
            legs = ours
        else:
            exact = [leg for leg in legs if abs(max(_float(leg.get("closedSize")), 0.0) - trade_qty) <= qty_tolerance]
            if exact:
                legs = [exact[-1]]  # latest exact-size match
            else:
                capped_legs: list[dict[str, Any]] = []
                cumulative_size = 0.0
                for leg in reversed(legs):  # latest-first (legs are ascending by createdTime)
                    if cumulative_size + qty_tolerance >= trade_qty:
                        break
                    capped_legs.append(leg)
                    cumulative_size += max(_float(leg.get("closedSize")), 0.0)
                if capped_legs:
                    # Restore ascending order so legs[-1] is the latest (close-completion) leg.
                    capped_legs.sort(key=lambda r: int(_float(r.get("createdTime") or r.get("updatedTime") or 0)))
                    legs = capped_legs
    # A Closed-PnL row proves only the quantity in ``closedSize``. A successful
    # but false-empty REST position snapshot plus a qty=1 sibling close must not
    # close a qty=3 ledger row. Require the selected close legs to cover the
    # ledger quantity whenever this matcher is the positive orphan evidence.
    # A private-WS size=0 event proves the venue is flat and suppresses retrying
    # a reduce-only exit, but it does not make a partial price/fee record
    # reconstructable. Keep the ledger row pending until all close legs arrive.
    if require_quantity_coverage and trade_qty > 0.0:
        selected_size = sum(max(_float(leg.get("closedSize")), 0.0) for leg in legs)
        qty_tolerance = max(trade_qty * 1e-8, 1e-12)
        if selected_size + qty_tolerance < trade_qty:
            return {}
    priced = [
        (_float(r.get("closedSize")), _float(r.get("avgExitPrice")))
        for r in legs
    ]
    weighted = [(size, price) for size, price in priced if size > 0.0 and price > 0.0]
    total_size = sum(size for size, _ in weighted)
    if total_size > 0.0:
        exit_price = sum(size * price for size, price in weighted) / total_size
    else:
        exit_price = _float(legs[-1].get("avgExitPrice"))
    if exit_price <= 0.0:
        return {}
    # The V5 Closed-PnL schema calls the exit fee ``closeFee``. ``execFee`` is
    # retained only as a compatibility fallback for historical/test fixtures.
    exit_fee_usdt = sum(
        _float(r.get("closeFee"))
        if r.get("closeFee") not in (None, "")
        else _float(r.get("execFee"))
        for r in legs
    )
    venue_open_fee_usdt = sum(_float(r.get("openFee")) for r in legs)
    venue_closed_pnl_rows = [
        _float(r.get("closedPnl"))
        for r in legs
        if r.get("closedPnl") not in (None, "")
    ]
    last_leg = legs[-1]
    # Bybit's createdTime IS the venue execution time; the close completes at
    # the last leg.
    closed_at_ms = int(_float(last_leg.get("createdTime") or last_leg.get("updatedTime") or now_ms)) or now_ms
    # A non-positive entry_price can't yield a meaningful return — record the venue
    # exit (evidence of the close) with a 0 return rather than a garbage one.
    gross_trade_return = _trade_return(entry_price, exit_price, side=side) if entry_price > 0.0 else 0.0
    notional_weight = _ratio_or_zero(trade.get("notional_usdt"), trade.get("equity_usdt"))
    backfill: dict[str, Any] = {
        "exit_price": exit_price,
        # Bybit documents Closed-PnL avgExitPrice as cost-influenced rather than
        # a raw execution VWAP. Keep the source explicit; execution history or
        # aggregate venue Closed-PnL is the forensic authority when available.
        "exit_price_source": "bybit_closed_pnl_avg_exit_cost_adjusted",
        "exit_fee_usdt": exit_fee_usdt,
        "venue_open_fee_allocated_usdt": venue_open_fee_usdt,
        "exit_exec_time_ms": closed_at_ms,
        "gross_trade_return": gross_trade_return,
        "net_return": gross_trade_return * notional_weight,
        "exit_ts_ms": closed_at_ms,
        "exit_trigger_ts_ms": closed_at_ms,
        "closed_at_ms": closed_at_ms,
        "submit_mode": "orphan_reconciled",
    }
    metadata_fields = {
        "venue_stop_order_type": "_venue_stop_order_type",
        "venue_create_type": "_venue_create_type",
        "venue_order_link_id": "_venue_order_link_id",
        "venue_order_status": "_venue_order_status",
    }
    for output_key, source_key in metadata_fields.items():
        values = sorted(
            {
                str(leg.get(source_key) or "")
                for leg in legs
                if str(leg.get(source_key) or "")
            }
        )
        if values:
            backfill[output_key] = ",".join(values)
    if venue_closed_pnl_rows:
        # Preserve Bybit's net Closed-PnL value beside the ledger's strategy-
        # return convention instead of silently conflating the two. On a netted
        # same-symbol component group this is a quantity-prorated allocation;
        # only the conserved group sum is authoritative component-agnostic truth.
        backfill["venue_closed_pnl_allocated_usdt"] = sum(venue_closed_pnl_rows)
    if len(legs) > 1:
        backfill["orphan_close_legs"] = len(legs)
    exit_order_id = str(last_leg.get("orderId") or "")
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

# --- risk-exit planning for the always-on risk service ---

def _price_crosses_stop(*, side: str, price: float, stop_price: float) -> bool:
    return price >= stop_price if side == "short" else price <= stop_price


def _price_crosses_take_profit(*, side: str, price: float, take_profit_price: float) -> bool:
    return price <= take_profit_price if side == "short" else price >= take_profit_price


def plan_risk_exits(
    open_trades: pl.DataFrame,
    *,
    position_by_symbol: dict[str, dict[str, Any]],
    price_by_symbol: dict[str, float],
    now_ms: int,
    cap_qty_to_trade: bool = False,
) -> list[dict[str, Any]]:
    if open_trades.is_empty():
        return []
    exits: list[dict[str, Any]] = []
    for trade in open_trades.to_dicts():
        symbol = str(trade.get("symbol", ""))
        position = position_by_symbol.get(symbol, {})
        side = str(trade.get("side") or _normalized_position_side(position.get("side")) or "short")
        # DEPLOY NOTE (reconcile-core-2): on the SHARED netted demo account (3 sleeves,
        # one account) position.size is the SUM of every sleeve's leg on this symbol.
        # Sizing a reduce-only risk exit to that netted size lets one sleeve's stop
        # FLATTEN a sibling sleeve's leg (reduce_only caps at the netted position, not
        # at this sleeve's leg). When cap_qty_to_trade (set by ws_risk whenever a
        # sibling-sleeve ledger is configured) cap the exit at min(trade.qty,
        # position.size) so a stop closes only this sleeve's exposure. Single-sleeve
        # symbols are unaffected (trade.qty == position.size). If trade.qty is transiently
        # smaller than the real position, the residual exposure beyond this sleeve's ledgered
        # qty is recovered by the separate untracked-position flatten path — NOT by a re-emit
        # here (plan_risk_exits only iterates OPEN ledger trades, so the just-closed trade is
        # gone next pass). Under-closing is fail-safe; the cross-sleeve OVER-close it prevents
        # is the non-self-healing failure. Off (default) preserves the legacy single-account
        # 'prefer venue size'.
        trade_qty = _float(trade.get("qty"))
        position_size = _float(position.get("size"))
        if cap_qty_to_trade:
            # ws-risk-3: in cap mode (a sibling-sleeve ledger is configured) the
            # exit qty is NEVER the raw netted position.size — that would let this
            # sleeve's stop flatten a sibling's leg, the non-self-healing
            # cross-sleeve OVER-close documented above. The ceiling is this
            # sleeve's own ledgered qty.
            #   trade_qty>0, position_size>0 -> min(trade_qty, position_size)
            #   trade_qty>0, position_size<=0 -> trade_qty (no venue size yet)
            #   trade_qty<=0 -> SKIP: a zero/empty ledger qty (schema drift,
            #     adopted/hedge row, partially-cleared row) must not fall back to
            #     the netted size and liquidate another sleeve. Under-closing is
            #     fail-safe; residual exposure is recovered by the separate
            #     untracked-position flatten path.
            if trade_qty <= 0.0:
                continue
            qty = _quantity_text(min(trade_qty, position_size) if position_size > 0.0 else trade_qty)
        else:
            qty = str(_first_non_empty(position.get("size"), trade.get("qty")))
        if not symbol or not qty:
            continue
        current_price = price_by_symbol.get(symbol, 0.0)
        exit_checks: list[tuple[int, int, str, float | None]] = []
        stop_price = _float(trade.get("stop_price"))
        if current_price > 0.0 and stop_price > 0.0 and _price_crosses_stop(side=side, price=current_price, stop_price=stop_price):
            exit_checks.append((now_ms, 0, "stop_loss", current_price))
        take_profit_price = _float(trade.get("take_profit_price"))
        if (
            current_price > 0.0
            and take_profit_price > 0.0
            and _price_crosses_take_profit(side=side, price=current_price, take_profit_price=take_profit_price)
        ):
            exit_checks.append((now_ms, 1, "take_profit", current_price))
        planned_exit_ts_ms = int(trade.get("planned_exit_ts_ms") or 0)
        if planned_exit_ts_ms > 0 and now_ms >= planned_exit_ts_ms:
            exit_checks.append((planned_exit_ts_ms, 2, "max_hold", current_price if current_price > 0.0 else None))
        if not exit_checks:
            continue
        trigger_ts_ms, _, reason, planned_price = sorted(exit_checks, key=lambda item: (item[0], item[1]))[0]
        exits.append(
            {
                "trade_id": str(trade["trade_id"]),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "exit_reason": reason,
                "exit_trigger_ts_ms": trigger_ts_ms,
                "planned_exit_price": planned_price if planned_price is not None else current_price,
                "planned_exit_ts_ms": planned_exit_ts_ms,
            }
        )
    return exits

def plan_stop_repairs(
    open_trades: pl.DataFrame,
    *,
    position_by_symbol: dict[str, dict[str, Any]],
    skip_symbols: set[str] | None = None,
    tolerance_bps: float = 1.0,
) -> list[dict[str, Any]]:
    if open_trades.is_empty():
        return []
    skip = skip_symbols or set()
    repairs: list[dict[str, Any]] = []
    for trade in open_trades.to_dicts():
        symbol = str(trade.get("symbol", ""))
        if not symbol or symbol in skip:
            continue
        position = position_by_symbol.get(symbol)
        if not position:
            continue
        stop_price = _float(trade.get("stop_price"))
        take_profit_price = _float(trade.get("take_profit_price"))
        current_stop = _first_float(position, ("stopLoss", "stop_loss", "sl", "stopLossPrice"))
        current_take_profit = _first_float(position, ("takeProfit", "take_profit", "tp", "takeProfitPrice"))
        needs_stop = stop_price > 0.0 and not _prices_close(current_stop, stop_price, tolerance_bps=tolerance_bps)
        needs_take_profit = take_profit_price > 0.0 and not _prices_close(
            current_take_profit,
            take_profit_price,
            tolerance_bps=tolerance_bps,
        )
        if not needs_stop and not needs_take_profit:
            continue
        repairs.append(
            {
                "trade_id": str(trade.get("trade_id", "")),
                "symbol": symbol,
                "side": str(trade.get("side") or _normalized_position_side(position.get("side")) or ""),
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "current_stop_price": current_stop,
                "current_take_profit_price": current_take_profit,
                "needs_stop_repair": needs_stop,
                "needs_take_profit_repair": needs_take_profit,
            }
        )
    return repairs
