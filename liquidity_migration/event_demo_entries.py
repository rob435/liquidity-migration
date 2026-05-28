"""Extracted from event_demo.py — see that module's docstring.

This sibling holds a cohesive slice of the event-demo machinery. It
imports shared helpers/configs from event_demo.py (the hub); the hub
re-imports this module's public names at the bottom so external callers
(`from liquidity_migration.event_demo import X`) keep working unchanged.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Callable




from .event_demo import (  # noqa: F401  (shared hub helpers)
    EventDemoCycleConfig,
    _decimal_text,
    _float,
    _order_link_id,
    _order_params,
    _prices_close,
    _split_order_link_id,
    _split_qty_for_max_order_size,
    _stop_price_for_entry,
    _take_profit_price_for_entry,
    _wait_for_execution_summary,
    order_quantity_for_notional,
)

_logger = logging.getLogger(__name__)


def _preflight_entry_order_row(
    *,
    entry_link: str,
    now_ms: int,
    candidate: dict[str, Any],
    strategy_id: str,
    symbol: str,
    bybit_side: str,
    side: str,
    order_type: str,
    qty: str,
    price: float,
    actual_notional: float,
    order_notional_pct_equity: float,
    entry_leverage: float,
    initial_margin_usdt: float,
    equity_usdt: float,
    tick_size: float,
    qty_step: float,
    stop_price: float,
    take_profit_price: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> dict[str, Any]:
    return {
        "order_link_id": entry_link,
        "ts_ms": now_ms,
        "trade_id": str(candidate["trade_id"]),
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": bybit_side,
        "order_type": order_type,
        "qty": qty,
        "reduce_only": False,
        "order_id": "",
        "submit_mode": "preflight",
        "avg_price": price,
        "notional_usdt": actual_notional,
        "target_notional_pct_equity": order_notional_pct_equity,
        "entry_leverage": entry_leverage,
        "initial_margin_usdt": initial_margin_usdt,
        "status": "submitted",
        "trade_side": side,
        "signal_ts_ms": int(candidate["signal_ts_ms"]),
        "entry_ready_ts_ms": int(candidate.get("entry_ready_ts_ms") or 0),
        "entry_policy": str(candidate.get("entry_policy", "")),
        "entry_rule": str(candidate.get("entry_rule", "")),
        "entry_quality_tier": str(candidate.get("entry_quality_tier", "")),
        "equity_usdt": equity_usdt,
        "tick_size": tick_size,
        "qty_step": qty_step,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "entry_stop_update_status": "",
        "entry_stop_update_error": "",
        "error": "",
    }

def _execute_entries(
    candidates: list[dict[str, Any]],
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    equity_usdt: float,
    order_notional_pct_equity: float,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
    private_client_factory: Callable[[], Any] | None = None,
    max_workers: int | None = None,
    execution_event_router: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not candidates:
        return [], []

    effective_workers = int(max_workers if max_workers is not None else demo.max_concurrent_entries)
    effective_workers = max(1, min(effective_workers, len(candidates)))

    if effective_workers <= 1 or private_client_factory is None or not demo.submit_orders:
        rows: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        for candidate in candidates:
            row, sub_orders = _execute_single_entry(
                candidate,
                trading_client=trading_client,
                demo=demo,
                equity_usdt=equity_usdt,
                order_notional_pct_equity=order_notional_pct_equity,
                price_by_symbol=price_by_symbol,
                contract_by_symbol=contract_by_symbol,
                now_ms=now_ms,
                strategy_id=strategy_id,
                record_preflight=record_preflight,
                execution_event_router=execution_event_router,
            )
            if row is not None:
                rows.append(row)
            orders.extend(sub_orders)
        return rows, orders

    # Parallel path. Each worker owns its own private REST client via the
    # factory — `requests.Session` (which pybit's HTTP wraps) is not safe to
    # share under heavy concurrent place_order load. Per-thread storage caches
    # the client across multiple candidates handled by the same worker, so the
    # TLS/auth handshake amortises.
    thread_local = threading.local()

    def _worker_client() -> Any:
        client = getattr(thread_local, "client", None)
        if client is None:
            client = private_client_factory()
            thread_local.client = client
        return client

    def _task(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        return _execute_single_entry(
            candidate,
            trading_client=_worker_client(),
            demo=demo,
            equity_usdt=equity_usdt,
            order_notional_pct_equity=order_notional_pct_equity,
            price_by_symbol=price_by_symbol,
            contract_by_symbol=contract_by_symbol,
            now_ms=now_ms,
            strategy_id=strategy_id,
            record_preflight=record_preflight,
            execution_event_router=execution_event_router,
        )

    parallel_rows: list[dict[str, Any]] = []
    parallel_orders: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=effective_workers) as executor:
        # Submit in candidate order, await in candidate order: preserves the
        # row/order list ordering callers rely on for deterministic dedup.
        futures = [executor.submit(_task, candidate) for candidate in candidates]
        for future in futures:
            row, sub_orders = future.result()
            if row is not None:
                parallel_rows.append(row)
            parallel_orders.extend(sub_orders)
    return parallel_rows, parallel_orders

def _execute_single_entry(
    candidate: dict[str, Any],
    *,
    trading_client: Any | None,
    demo: EventDemoCycleConfig,
    equity_usdt: float,
    order_notional_pct_equity: float,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
    execution_event_router: Any | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    symbol = str(candidate["symbol"])
    price = price_by_symbol.get(symbol)
    contract = contract_by_symbol.get(symbol, {})
    if price is None or price <= 0.0:
        return None, []
    tick_size = _float(contract.get("tick_size")) or 0.0001
    qty_step = _float(contract.get("qty_step")) or 0.001
    capped_notional = equity_usdt * demo.wallet_balance_fraction * order_notional_pct_equity
    # Compute the UNCAPPED target qty. We deliberately do NOT pass
    # max_order_qty here -- when the venue's per-order cap binds, we split
    # the entry into N sub-orders (each under the cap) below rather than
    # reducing the trade's total notional. This eliminates a live-vs-backtest
    # divergence where capped trades silently under-sized live exposure
    # (observed REQUSDT live at 53% of target notional).
    max_qty_per_order = (
        _float(contract.get("max_market_order_qty"))
        or _float(contract.get("max_order_qty"))
    )
    quantity = order_quantity_for_notional(
        notional_usdt=capped_notional,
        price=price,
        qty_step=qty_step,
        min_order_qty=_float(contract.get("min_order_qty")),
        min_notional_value=_float(contract.get("min_notional_value")),
        max_order_qty=0.0,
    )
    if quantity is None:
        _logger.info(
            "entry sizing rejected symbol=%s notional=%.2f price=%.6g "
            "qty_step=%s min_qty=%s min_notional=%s",
            symbol,
            capped_notional,
            price,
            qty_step,
            _float(contract.get("min_order_qty")) or "-",
            _float(contract.get("min_notional_value")) or "-",
        )
        return None, []
    qty, actual_notional = quantity
    # Split the target qty across N sub-orders if the per-order cap binds.
    target_qty_decimal = Decimal(qty)
    sub_qty_decimals = _split_qty_for_max_order_size(
        target_qty=target_qty_decimal,
        max_qty_per_order=max_qty_per_order,
        qty_step=qty_step,
    )
    sub_qty_strs = [_decimal_text(q) for q in sub_qty_decimals]
    if len(sub_qty_strs) > 1:
        _logger.info(
            "entry split into %d sub-orders symbol=%s target_qty=%s "
            "max_mkt_qty=%s sub_qtys=%s",
            len(sub_qty_strs),
            symbol,
            qty,
            max_qty_per_order,
            sub_qty_strs,
        )
    # demo.entry_leverage > 0 is a config invariant — _validate_demo_config
    # rejects non-positive values at parse time, so no zero-guard here.
    initial_margin_usdt = actual_notional / demo.entry_leverage
    side = str(candidate["side"])
    bybit_side = "Sell" if side == "short" else "Buy"
    stop_loss_pct = float(candidate.get("stop_loss_pct") or 0.12)
    take_profit_pct = float(candidate.get("take_profit_pct") or 0.0)
    stop_price = _stop_price_for_entry(
        entry_price=price,
        side=side,
        stop_loss_pct=stop_loss_pct,
        tick_size=tick_size,
    )
    take_profit_price = _take_profit_price_for_entry(
        entry_price=price,
        side=side,
        take_profit_pct=take_profit_pct,
        tick_size=tick_size,
    )
    entry_link = _order_link_id("en", symbol=symbol, signal_ts_ms=int(candidate["signal_ts_ms"]))
    first_order_result: dict[str, Any] = {}
    protection_update_status = ""
    protection_update_error = ""
    submit_mode = "dry_run"
    order_status = "planned"
    error = ""
    target_qty_total = sum(_float(s) for s in sub_qty_strs)
    # Defaults: dry-run "fills" the full target at the candidate price (so the
    # trade row reflects what would have happened); live path starts at zero
    # filled and only accumulates from real venue fills, so a place_order or
    # set_leverage failure leaves filled_qty=0 and the trade row is not built.
    if demo.submit_orders:
        filled_qty = 0.0
        entry_price = price
        filled_notional = 0.0
    else:
        filled_qty = target_qty_total
        entry_price = price
        filled_notional = actual_notional
    sub_order_rows: list[dict[str, Any]] = []
    # Defined here so BOTH the submit_orders branch and the dry-run branch
    # (which never enters the per-sub fill loop) leave the trade-row builder
    # able to read these without a NameError.
    total_fee_acc = 0.0
    max_exec_time_ms_acc = 0

    def _sub_link(idx: int) -> str:
        # Single-order path keeps the legacy entry_link verbatim for
        # backward compat with tests + reconciler matching.
        return entry_link if len(sub_qty_strs) == 1 else _split_order_link_id(entry_link, idx)

    if demo.submit_orders:
        assert trading_client is not None
        try:
            trading_client.set_leverage(symbol=symbol, buy_leverage=demo.entry_leverage, sell_leverage=demo.entry_leverage)
        except Exception as exc:  # noqa: BLE001 - failed entries must be ledgered without aborting the cycle
            submit_mode = "error"
            order_status = "failed"
            error = f"set_leverage failed: {exc}"[:500]
            filled_qty = 0.0
            filled_notional = 0.0
            # Emit a placeholder order row so the failure is auditable in the
            # ledger (mirrors pre-split behaviour where one order_row was
            # ALWAYS produced per candidate, even on set_leverage failure).
            sub_order_rows.append({
                "order_link_id": entry_link,
                "ts_ms": now_ms,
                "trade_id": str(candidate["trade_id"]),
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": bybit_side,
                "order_type": demo.entry_order_type,
                "qty": qty,
                "reduce_only": False,
                "order_id": "",
                "submit_mode": submit_mode,
                "avg_price": 0.0,
                "notional_usdt": 0.0,
                "target_notional_pct_equity": order_notional_pct_equity,
                "entry_leverage": demo.entry_leverage,
                "initial_margin_usdt": 0.0,
                "status": order_status,
                "trade_side": side,
                "signal_ts_ms": int(candidate["signal_ts_ms"]),
                "entry_ready_ts_ms": int(candidate.get("entry_ready_ts_ms") or 0),
                "entry_policy": str(candidate.get("entry_policy", "")),
                "entry_rule": str(candidate.get("entry_rule", "")),
                "entry_quality_tier": str(candidate.get("entry_quality_tier", "")),
                "equity_usdt": equity_usdt,
                "tick_size": tick_size,
                "qty_step": qty_step,
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "entry_stop_update_status": "",
                "entry_stop_update_error": "",
                "error": error,
            })

        # total_fee_acc / max_exec_time_ms_acc are pre-initialized at function
        # scope above; total_filled_qty_acc / total_fill_value_acc are local to
        # the submit branch only.
        total_filled_qty_acc = 0.0
        total_fill_value_acc = 0.0
        if not error:
            for idx, sub_qty_str in enumerate(sub_qty_strs):
                is_first = idx == 0
                sub_link = _sub_link(idx)
                sub_target = _float(sub_qty_str)
                sub_actual_notional = sub_target * price
                sub_initial_margin = sub_actual_notional / demo.entry_leverage if demo.entry_leverage > 0.0 else 0.0

                if record_preflight is not None:
                    record_preflight(
                        _preflight_entry_order_row(
                            entry_link=sub_link,
                            now_ms=now_ms,
                            candidate=candidate,
                            strategy_id=strategy_id,
                            symbol=symbol,
                            bybit_side=bybit_side,
                            side=side,
                            order_type=demo.entry_order_type,
                            qty=sub_qty_str,
                            price=price,
                            actual_notional=sub_actual_notional,
                            order_notional_pct_equity=order_notional_pct_equity,
                            entry_leverage=demo.entry_leverage,
                            initial_margin_usdt=sub_initial_margin,
                            equity_usdt=equity_usdt,
                            tick_size=tick_size,
                            qty_step=qty_step,
                            stop_price=stop_price,
                            take_profit_price=take_profit_price,
                            stop_loss_pct=stop_loss_pct,
                            take_profit_pct=take_profit_pct,
                        )
                    )

                # Bybit stops are position-level — attach to the FIRST sub-order
                # only, so subsequent subs just add qty to the same position
                # under the same protective stop. Avoids redundant set-trading-stop
                # calls and matches Bybit's actual semantics.
                sub_order_params = _order_params(
                    symbol=symbol,
                    side=bybit_side,
                    qty=sub_qty_str,
                    order_type=demo.entry_order_type,
                    order_link_id=sub_link,
                    reduce_only=False,
                    stop_loss=stop_price if is_first else None,
                    take_profit=(take_profit_price if take_profit_price > 0.0 else None) if is_first else None,
                )

                sub_order_result: dict[str, Any] = {}
                sub_submit_mode = "submitted"
                sub_error = ""
                try:
                    sub_order_result = trading_client.place_order(**sub_order_params)
                    submit_mode = "submitted"
                    if is_first:
                        first_order_result = sub_order_result
                except Exception as exc:  # noqa: BLE001
                    sub_submit_mode = "error"
                    sub_error = f"place_order failed: {exc}"[:500]
                    # First-sub place_order failure mirrors the legacy single-order
                    # behaviour: trade-level error + submitted_unconfirmed status so
                    # reconciliation adopts any actual venue-side fill.
                    if is_first and not error:
                        submit_mode = "error"
                        order_status = "submitted_unconfirmed"
                        error = sub_error
                    sub_order_rows.append({
                        "order_link_id": sub_link,
                        "ts_ms": now_ms,
                        "trade_id": str(candidate["trade_id"]),
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "side": bybit_side,
                        "order_type": demo.entry_order_type,
                        "qty": sub_qty_str,
                        "reduce_only": False,
                        "order_id": "",
                        "submit_mode": sub_submit_mode,
                        "avg_price": 0.0,
                        "notional_usdt": 0.0,
                        "target_notional_pct_equity": order_notional_pct_equity,
                        "entry_leverage": demo.entry_leverage,
                        "initial_margin_usdt": 0.0,
                        "status": "submitted_unconfirmed",
                        "trade_side": side,
                        "signal_ts_ms": int(candidate["signal_ts_ms"]),
                        "entry_ready_ts_ms": int(candidate.get("entry_ready_ts_ms") or 0),
                        "entry_policy": str(candidate.get("entry_policy", "")),
                        "entry_rule": str(candidate.get("entry_rule", "")),
                        "entry_quality_tier": str(candidate.get("entry_quality_tier", "")),
                        "equity_usdt": equity_usdt,
                        "tick_size": tick_size,
                        "qty_step": qty_step,
                        "stop_price": stop_price,
                        "take_profit_price": take_profit_price,
                        "stop_loss_pct": stop_loss_pct,
                        "take_profit_pct": take_profit_pct,
                        "entry_stop_update_status": "",
                        "entry_stop_update_error": "",
                        "error": sub_error,
                    })
                    continue

                sub_filled_qty = 0.0
                sub_avg_price = 0.0
                sub_status = "submitted_unconfirmed"
                try:
                    sub_exec = _wait_for_execution_summary(
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
                    if is_first and not error:
                        error = sub_error
                        order_status = "submitted_unconfirmed"
                    sub_fee = 0.0
                    sub_exec_time_ms = 0
                else:
                    sub_filled_qty = _float(sub_exec.get("qty"))
                    sub_avg_price = _float(sub_exec.get("avg_price")) or price
                    sub_fee = _float(sub_exec.get("fee"))
                    sub_exec_time_ms = int(_float(sub_exec.get("exec_time_ms") or 0))
                    sub_tolerance = max(sub_target * 1e-8, 1e-12)
                    if sub_target > 0.0 and sub_filled_qty + sub_tolerance >= sub_target:
                        sub_status = "filled"
                    elif sub_filled_qty > 0.0:
                        sub_status = "partial"
                    else:
                        sub_status = "submitted_unconfirmed"

                total_filled_qty_acc += sub_filled_qty
                total_fill_value_acc += sub_avg_price * sub_filled_qty
                total_fee_acc += sub_fee
                if sub_exec_time_ms > max_exec_time_ms_acc:
                    max_exec_time_ms_acc = sub_exec_time_ms

                sub_filled_str = _decimal_text(Decimal(str(sub_filled_qty))) if sub_filled_qty > 0.0 else ""
                sub_notional = abs(sub_avg_price * sub_filled_qty) if sub_filled_qty > 0.0 else 0.0
                sub_filled_initial_margin = sub_notional / demo.entry_leverage if demo.entry_leverage > 0.0 else 0.0
                sub_order_rows.append({
                    "order_link_id": sub_link,
                    "ts_ms": now_ms,
                    "trade_id": str(candidate["trade_id"]),
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "side": bybit_side,
                    "order_type": demo.entry_order_type,
                    "qty": sub_filled_str or sub_qty_str,
                    "reduce_only": False,
                    "order_id": sub_order_result.get("orderId", ""),
                    "submit_mode": sub_submit_mode,
                    "avg_price": sub_avg_price,
                    "fee_usdt": sub_fee,
                    "exec_time_ms": sub_exec_time_ms,
                    "notional_usdt": sub_notional,
                    "target_notional_pct_equity": order_notional_pct_equity,
                    "entry_leverage": demo.entry_leverage,
                    "initial_margin_usdt": sub_filled_initial_margin,
                    "status": sub_status,
                    "trade_side": side,
                    "signal_ts_ms": int(candidate["signal_ts_ms"]),
                    "entry_ready_ts_ms": int(candidate.get("entry_ready_ts_ms") or 0),
                    "entry_policy": str(candidate.get("entry_policy", "")),
                    "entry_rule": str(candidate.get("entry_rule", "")),
                    "entry_quality_tier": str(candidate.get("entry_quality_tier", "")),
                    "equity_usdt": equity_usdt,
                    "tick_size": tick_size,
                    "qty_step": qty_step,
                    "stop_price": stop_price,
                    "take_profit_price": take_profit_price,
                    "stop_loss_pct": stop_loss_pct,
                    "take_profit_pct": take_profit_pct,
                    "entry_stop_update_status": "",
                    "entry_stop_update_error": "",
                    "error": sub_error,
                })

            # Aggregate the per-sub fills into the trade-level entry state.
            if submit_mode == "submitted":
                filled_qty = total_filled_qty_acc
                if total_filled_qty_acc > 0.0:
                    entry_price = total_fill_value_acc / total_filled_qty_acc
                else:
                    entry_price = price
                filled_notional = abs(entry_price * filled_qty) if filled_qty > 0.0 else 0.0
                qty_tolerance = max(target_qty_total * 1e-8, 1e-12)
                if target_qty_total > 0.0 and filled_qty + qty_tolerance >= target_qty_total:
                    order_status = "filled"
                elif filled_qty > 0.0:
                    order_status = "partial"
                else:
                    order_status = "submitted_unconfirmed"

            if filled_qty > 0.0:
                # Stop-repair using the AGGREGATE avg fill price across all sub-orders.
                filled_stop_price = _stop_price_for_entry(
                    entry_price=entry_price,
                    side=side,
                    stop_loss_pct=stop_loss_pct,
                    tick_size=tick_size,
                )
                filled_take_profit_price = _take_profit_price_for_entry(
                    entry_price=entry_price,
                    side=side,
                    take_profit_pct=take_profit_pct,
                    tick_size=tick_size,
                )
                if not _prices_close(stop_price, filled_stop_price, tolerance_bps=0.0) or (
                    filled_take_profit_price > 0.0
                    and not _prices_close(take_profit_price, filled_take_profit_price, tolerance_bps=0.0)
                ):
                    try:
                        trading_client.set_trading_stop(
                            symbol=symbol,
                            stop_loss=_decimal_text(Decimal(str(filled_stop_price)))
                            if filled_stop_price > 0.0
                            else None,
                            take_profit=_decimal_text(Decimal(str(filled_take_profit_price)))
                            if filled_take_profit_price > 0.0
                            else None,
                        )
                        protection_update_status = "submitted"
                    except Exception as exc:  # noqa: BLE001 - venue repair daemon will retry from ledger state
                        protection_update_status = "failed"
                        protection_update_error = str(exc)[:500]
                stop_price = filled_stop_price
                take_profit_price = filled_take_profit_price
                # Propagate the stop-repair status onto every sub-order row so
                # ledger consumers can audit the protection state per-sub.
                for sub_row in sub_order_rows:
                    sub_row["entry_stop_update_status"] = protection_update_status
                    sub_row["entry_stop_update_error"] = protection_update_error
                    sub_row["stop_price"] = stop_price
                    sub_row["take_profit_price"] = take_profit_price

    else:
        # Dry-run / record-only: one order row per sub-qty, no venue calls.
        for idx, sub_qty_str in enumerate(sub_qty_strs):
            sub_link = _sub_link(idx)
            sub_target = _float(sub_qty_str)
            sub_actual_notional = sub_target * price
            sub_initial_margin = sub_actual_notional / demo.entry_leverage if demo.entry_leverage > 0.0 else 0.0
            sub_order_rows.append({
                "order_link_id": sub_link,
                "ts_ms": now_ms,
                "trade_id": str(candidate["trade_id"]),
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": bybit_side,
                "order_type": demo.entry_order_type,
                "qty": sub_qty_str,
                "reduce_only": False,
                "order_id": "",
                "submit_mode": "dry_run",
                "avg_price": price,
                "notional_usdt": sub_actual_notional,
                "target_notional_pct_equity": order_notional_pct_equity,
                "entry_leverage": demo.entry_leverage,
                "initial_margin_usdt": sub_initial_margin,
                "status": "planned",
                "trade_side": side,
                "signal_ts_ms": int(candidate["signal_ts_ms"]),
                "entry_ready_ts_ms": int(candidate.get("entry_ready_ts_ms") or 0),
                "entry_policy": str(candidate.get("entry_policy", "")),
                "entry_rule": str(candidate.get("entry_rule", "")),
                "entry_quality_tier": str(candidate.get("entry_quality_tier", "")),
                "equity_usdt": equity_usdt,
                "tick_size": tick_size,
                "qty_step": qty_step,
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "entry_stop_update_status": "",
                "entry_stop_update_error": "",
                "error": "",
            })

    entry_qty = _decimal_text(Decimal(str(filled_qty))) if filled_qty > 0.0 else ""
    filled_initial_margin_usdt = filled_notional / demo.entry_leverage if demo.entry_leverage > 0.0 else 0.0
    trade_row: dict[str, Any] | None = None
    if not demo.submit_orders or filled_qty > 0.0:
        trade_row = {
            **candidate,
            "ts_ms": now_ms,
            "strategy_id": strategy_id,
            "status": "open",
            "entry_ts_ms": now_ms,
            # Venue-reported fill time (latest execTime across sub-orders);
            # 0 in the dry-run branch since no venue executions occurred.
            # Paper↔demo reconciliation uses this to measure true fill-time
            # skew, vs. entry_ts_ms which is the cycle wall-clock.
            "entry_exec_time_ms": max_exec_time_ms_acc,
            "entry_fee_usdt": total_fee_acc,
            "entry_price": entry_price,
            "qty": entry_qty or qty,
            "notional_usdt": filled_notional if demo.submit_orders else actual_notional,
            "equity_usdt": equity_usdt,
            "target_notional_pct_equity": order_notional_pct_equity,
            "entry_leverage": demo.entry_leverage,
            "initial_margin_usdt": filled_initial_margin_usdt if demo.submit_orders else initial_margin_usdt,
            "initial_margin_pct_equity": (filled_initial_margin_usdt if demo.submit_orders else initial_margin_usdt) / equity_usdt
            if equity_usdt > 0.0
            else 0.0,
            "tick_size": tick_size,
            "qty_step": qty_step,
            # Persist the venue's per-order market cap on the trade so the
            # exit path can split a close-position market order into N
            # sub-orders mirroring the entry split (without this, exits on
            # positions built up by entry-split would be rejected by Bybit
            # for exceeding ``maxMktOrderQty`` on a single reduce-only order).
            "max_market_order_qty": max_qty_per_order,
            "stop_price": stop_price,
            "take_profit_price": take_profit_price,
            "entry_stop_update_status": protection_update_status,
            "entry_stop_update_error": protection_update_error,
            "entry_order_link_id": entry_link,
            "entry_order_id": first_order_result.get("orderId", ""),
            "submit_mode": submit_mode,
            "opened_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
    return trade_row, sub_order_rows
