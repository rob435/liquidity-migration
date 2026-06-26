from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.continuous_rebalance import ContinuousRebalanceResizePlan
from liquidity_migration.continuous_rebalance_ledger import build_rebalance_resize_rows, prepare_rebalance_resize_order


def test_build_rebalance_resize_rows_keeps_submitted_target_qty_on_partial_fill() -> None:
    trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "strategy_id": "S",
                "symbol": "WIFUSDT",
                "side": "short",
                "sleeve": "continuous",
                "status": "open",
                "entry_price": 100.0,
                "qty": "2",
                "notional_usdt": 200.0,
                "equity_usdt": 10_000.0,
                "qty_step": 0.1,
            }
        ]
    )
    plan = ContinuousRebalanceResizePlan(
        trade_id="t1",
        symbol="WIFUSDT",
        side="Buy",
        reduce_only=True,
        qty=0.5,
        current_notional_usdt=200.0,
        target_notional_usdt=150.0,
        delta_notional_usdt=-50.0,
        reason="rebalance_reduce",
    )

    rows, orders = build_rebalance_resize_rows(
        [plan],
        trades,
        price_by_symbol={"WIFUSDT": 90.0},
        contract_by_symbol={"WIFUSDT": {"qty_step": 0.1}},
        now_ms=1_700_000_000_000,
        strategy_id="S",
        entry_link_prefix="en-c",
        exit_link_prefix="ux-c",
        default_sleeve="continuous",
        execution_by_trade_id={
            "t1": {
                "order_link_id": "lm-ux-c-WIFUSDT-filled",
                "qty": 0.25,
                "target_qty": "0.5",
                "avg_price": 92.0,
                "fee_usdt": 0.01,
                "exec_time_ms": 123,
                "order_id": "oid-1",
                "submit_mode": "submitted",
                "status": "partial",
            }
        },
    )

    assert rows[0]["qty"] == "1.75"
    assert rows[0]["rebalance_realized_return"] == pytest.approx(0.0002)
    assert orders[0]["qty"] == "0.25"
    assert orders[0]["target_qty"] == "0.5"
    assert orders[0]["filled_qty"] == "0.25"
    assert orders[0]["order_link_id"] == "lm-ux-c-WIFUSDT-filled"


def test_prepare_rebalance_resize_order_caps_reduce_qty_and_builds_preflight() -> None:
    plan = ContinuousRebalanceResizePlan(
        trade_id="t1",
        symbol="WIFUSDT",
        side="Buy",
        reduce_only=True,
        qty=5.0,
        current_notional_usdt=200.0,
        target_notional_usdt=0.0,
        delta_notional_usdt=-1_000.0,
        reason="rebalance_reduce",
    )
    trade = {
        "trade_id": "t1",
        "symbol": "WIFUSDT",
        "side": "short",
        "sleeve": "continuous",
        "qty": "2",
        "qty_step": 0.1,
    }

    prepared = prepare_rebalance_resize_order(
        plan,
        trade,
        price_by_symbol={"WIFUSDT": 100.0},
        contract_by_symbol={"WIFUSDT": {"qty_step": 0.1, "min_order_qty": 0.1}},
        now_ms=1_700_000_000_000,
        strategy_id="S",
        entry_link_prefix="en-c",
        exit_link_prefix="ux-c",
        default_sleeve="continuous",
    )

    assert prepared is not None
    assert prepared.qty == "2"
    assert prepared.planned_notional == pytest.approx(200.0)
    assert prepared.preflight["target_qty"] == "2"
    assert prepared.preflight["reduce_only"] is True
    assert prepared.preflight["exit_reason"] == "rebalance_reduce"
    assert prepared.preflight["order_link_id"].startswith("lm-ux-c-")
