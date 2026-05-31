"""Event-demo exits tests — split from the monolithic test_liquidity_migration_event_demo.py."""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest

from liquidity_migration.config import ResearchConfig
from liquidity_migration.event_demo import (
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    PENDING_ORDER_GUARD_MS,
    _execute_exits,
    _execute_risk_exits,
    _orphan_close_pnl_backfill,
    _preflight_exit_order_row,
    _reconcile_open_trades,
    _risk_reconcile_missing_positions,
    _limit_chase_price,
    _reconcile_pending_order_fills,
    _submit_reduce_only_exit,
    _terminalize_stale_pending_entry_orders,
    run_event_risk_cycle,
)
from liquidity_migration.storage import read_dataset, write_dataset

from _event_demo_fixtures import *  # noqa: F401,F403  (shared fakes/helpers)
from _event_demo_fixtures import (  # noqa: F401  explicit for the linters
    FailingKlineMarket,
    FakeKlineMarket,
    FakeRiskClient,
    MinimalEventMarket,
    _ClosedPnlClient,
    _RecordingInstrumentsMarket,
    _feature_cache_klines,
    _feature_cache_universe,
    _make_instruments_frame,
    _make_tickers_frame,
    _open_trade_row,
    _patch_minimal_event_cycle,
)


def test_limit_chase_price_crosses_spread_with_tick_rounding() -> None:
    assert _limit_chase_price(bybit_side="Buy", reference_price=100.0, bps=10.0, tick_size=0.1) == 100.1
    assert _limit_chase_price(bybit_side="Sell", reference_price=100.0, bps=10.0, tick_size=0.1) == 99.9


def test_limit_chase_uses_ioc_limits_then_market_fallback() -> None:
    client = FakeRiskClient(fill_market_orders=True)

    submit = _submit_reduce_only_exit(
        symbol="AAAUSDT",
        bybit_side="Buy",
        qty="1",
        trading_client=client,
        risk=EventRiskCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            exit_order_mode="limit_chase",
            limit_chase_attempts=2,
            limit_chase_wait_seconds=0.0,
        ),
        now_ms=1_700_000_000_000,
        reference_price=100.0,
        tick_size=0.1,
    )

    assert [row["orderType"] for row in client.orders] == ["Limit", "Limit", "Market"]
    assert client.orders[0]["timeInForce"] == "IOC"
    assert client.orders[0]["reduceOnly"] is True
    assert client.orders[0]["price"] == "100.1"
    assert submit["exec_summary"]["qty"] == "1"
    assert [row["status"] for row in submit["order_rows"]] == ["unfilled", "unfilled", "filled"]
    assert submit["order_rows"][-1]["target_qty"] == "1"
    assert submit["order_rows"][-1]["filled_qty"] == "1"


def test_market_risk_exit_history_error_stays_pending() -> None:
    client = FakeRiskClient(fail_trade_history=True)

    submit = _submit_reduce_only_exit(
        symbol="AAAUSDT",
        bybit_side="Buy",
        qty="1",
        trading_client=client,
        risk=EventRiskCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            exit_order_mode="market",
        ),
        now_ms=1_700_000_000_000,
        reference_price=100.0,
        tick_size=0.1,
    )

    assert submit["order_id"] == "order-1"
    assert submit["exec_summary"]["qty"] == ""
    assert submit["order_rows"][0]["status"] == "submitted_unconfirmed"
    assert "fill confirmation failed" in submit["order_rows"][0]["error"]


def test_limit_chase_history_error_stops_before_fallback() -> None:
    client = FakeRiskClient(fail_trade_history=True)

    submit = _submit_reduce_only_exit(
        symbol="AAAUSDT",
        bybit_side="Buy",
        qty="1",
        trading_client=client,
        risk=EventRiskCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            exit_order_mode="limit_chase",
            limit_chase_attempts=2,
            limit_chase_wait_seconds=0.0,
        ),
        now_ms=1_700_000_000_000,
        reference_price=100.0,
        tick_size=0.1,
    )

    assert [row["orderType"] for row in client.orders] == ["Limit"]
    assert submit["order_id"] == "order-1"
    assert submit["exec_summary"]["qty"] == ""
    assert submit["order_rows"][0]["status"] == "submitted_unconfirmed"
    assert "fill confirmation failed" in submit["order_rows"][0]["error"]


def test_event_exit_does_not_close_until_fill_confirmed() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows == []
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["notional_usdt"] == 0.0


def test_event_exit_records_order_error_without_raising() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fail_order_symbols={"AAAUSDT"}),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows == []
    assert orders[0]["status"] == "failed"
    assert orders[0]["submit_mode"] == "error"
    assert orders[0]["order_id"] == ""
    assert "place_order failed" in orders[0]["error"]
    assert "order rejected" in orders[0]["error"]


def test_event_exit_fill_confirmation_error_stays_pending() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fail_trade_history=True),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows == []
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["submit_mode"] == "submitted"
    assert orders[0]["order_id"] == "order-1"
    assert "fill confirmation failed" in orders[0]["error"]
    assert "history unavailable" in orders[0]["error"]


def test_event_exit_partial_fill_reduces_trade_qty_immediately() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-ex-",), fill_qty="0.4"),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows[0]["status"] == "open"
    assert rows[0]["qty"] == "0.6"
    assert rows[0]["notional_usdt"] == 60.0
    assert rows[0]["partial_exit_reason"] == "max_hold"
    assert rows[0]["partial_exit_qty"] == "0.4"
    assert orders[0]["status"] == "partial"
    assert orders[0]["filled_qty"] == "0.4"


def test_event_exit_closes_after_confirmed_fill() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-ex-",)),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows[0]["status"] == "closed"
    assert rows[0]["exit_price"] == 100.5
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled_qty"] == "1"


def test_pending_entry_fill_reconciles_to_open_trade() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 0.0,
                "target_notional_pct_equity": 0.2,
                "entry_leverage": 2.0,
                "initial_margin_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "trade_side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "equity_usdt": 10_000.0,
                "tick_size": 0.1,
                "qty_step": 0.1,
                "stop_price": 112.0,
                "take_profit_price": 80.0,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["status"] == "open"
    assert trades[0]["qty"] == "1"
    assert trades[0]["entry_price"] == 100.5
    assert trades[0]["stop_price"] == 112.0
    assert order_updates[0]["status"] == "filled"
    assert order_updates[0]["filled_qty"] == "1"


def test_pending_entry_split_recovery_sums_sub_order_fills() -> None:
    """BUG-3: s0 already built the open trade (qty=1 @100). s1 — a non-first
    sub-order sharing the trade_id, recovered here after a place_order-after-accept
    transport error — fills another 1. Its per-link fill (1) is NOT greater than
    the trade qty already carrying s0 (1), so the old `filled_qty > existing.qty`
    gate dropped it and the ledger under-reported the split position. The recovered
    leg must SUM: qty 1 + 1 = 2 with a value-weighted blended entry."""
    existing = pl.DataFrame(
        [
            {
                "trade_id": "t-split",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 100.0,
                "entry_leverage": 2.0,
                "equity_usdt": 10_000.0,
                "entry_ts_ms": 1_700_000_000_000,
            }
        ]
    )
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-split-s1",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t-split",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-s1",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 0.0,
                "target_notional_pct_equity": 0.2,
                "entry_leverage": 2.0,
                "initial_margin_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "trade_side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "equity_usdt": 10_000.0,
                "tick_size": 0.1,
                "qty_step": 0.1,
                "stop_price": 112.0,
                "take_profit_price": 80.0,
                "filled_qty": "",  # previous_filled_qty == 0
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    trades, _order_updates = _reconcile_pending_order_fills(
        orders,
        existing,
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert len(trades) == 1
    assert trades[0]["trade_id"] == "t-split"
    assert trades[0]["qty"] == "2"  # summed, NOT overwritten-when-greater
    assert trades[0]["entry_price"] == 100.25  # value-weighted blend of 100 and 100.5
    assert trades[0]["notional_usdt"] == pytest.approx(100.25 * 2)


def test_pending_entry_fill_recomputes_protection_from_confirmed_fill() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 0.0,
                "target_notional_pct_equity": 0.2,
                "entry_leverage": 2.0,
                "initial_margin_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "trade_side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "equity_usdt": 10_000.0,
                "tick_size": 0.1,
                "qty_step": 0.1,
                "stop_price": 112.0,
                "take_profit_price": 80.0,
                "stop_loss_pct": 0.12,
                "take_profit_pct": 0.20,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["entry_price"] == 100.5
    assert trades[0]["stop_price"] == 112.6
    assert trades[0]["take_profit_price"] == 80.4
    assert trades[0]["entry_stop_update_status"] == "submitted"
    # Schema completeness: even the entry-trade row written by the pending-fill
    # reconciler carries the entry_fee + entry_exec_time keys so downstream
    # reconciliation never trips on a missing column.
    assert "entry_fee_usdt" in trades[0]
    assert "entry_exec_time_ms" in trades[0]
    assert order_updates[0]["stop_price"] == 112.6
    assert order_updates[0]["take_profit_price"] == 80.4
    assert order_updates[0]["entry_stop_update_status"] == "submitted"
    assert "fee_usdt" in order_updates[0]
    assert "exec_time_ms" in order_updates[0]
    assert client.stop_updates == [{"symbol": "AAAUSDT", "stop_loss": "112.6", "take_profit": "80.4"}]


def test_pending_fill_history_error_keeps_order_pending() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 0.0,
                "status": "submitted_unconfirmed",
            }
        ]
    )
    client = FakeRiskClient(fail_trade_history=True)

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades == []
    assert order_updates[0]["status"] == "submitted_unconfirmed"
    assert order_updates[0]["order_link_id"] == "lm-en-pending"
    assert "fill reconciliation failed" in order_updates[0]["error"]
    assert "history unavailable" in order_updates[0]["error"]


def test_stale_pending_order_fill_is_not_polled_forever() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-stale",
                "ts_ms": 1_700_000_000_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "qty": "1",
                "reduce_only": False,
                "status": "submitted_unconfirmed",
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_000_000 + 16 * 60_000,
    )

    assert trades == []
    assert order_updates == []
    assert client.trade_history_calls == []


def test_stale_pending_order_fill_reconciles_when_live_position_exists() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-stale-live",
                "ts_ms": 1_700_000_000_000,
                "trade_id": "t-live",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-live",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 0.0,
                "target_notional_pct_equity": 0.2,
                "entry_leverage": 2.0,
                "initial_margin_usdt": 0.0,
                "status": "submitted_unconfirmed",
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
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_000_000 + 16 * 60_000,
        live_position_symbols={"AAAUSDT"},
    )

    assert client.trade_history_calls == ["lm-en-stale-live"]
    assert trades[0]["status"] == "open"
    assert trades[0]["trade_id"] == "t-live"
    assert trades[0]["qty"] == "1"
    assert order_updates[0]["status"] == "filled"
    assert order_updates[0]["filled_qty"] == "1"


def test_stale_pending_entry_terminalizes_only_when_exchange_flat() -> None:
    now_ms = 1_700_000_000_000
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-stale-flat",
                "ts_ms": now_ms - PENDING_ORDER_GUARD_MS - 1,
                "trade_id": "t-flat",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "qty": "1",
                "reduce_only": False,
                "status": "submitted_unconfirmed",
            },
            {
                "order_link_id": "lm-en-fresh",
                "ts_ms": now_ms - PENDING_ORDER_GUARD_MS,
                "trade_id": "t-fresh",
                "symbol": "BBBUSDT",
                "side": "Sell",
                "qty": "1",
                "reduce_only": False,
                "status": "submitted_unconfirmed",
            },
            {
                "order_link_id": "lm-ex-stale",
                "ts_ms": now_ms - PENDING_ORDER_GUARD_MS - 1,
                "trade_id": "t-exit",
                "symbol": "CCCUSDT",
                "side": "Buy",
                "qty": "1",
                "reduce_only": True,
                "status": "submitted_unconfirmed",
            },
        ]
    )

    updates = _terminalize_stale_pending_entry_orders(
        orders,
        live_position_symbols=set(),
        live_open_entry_order_symbols=set(),
        now_ms=now_ms,
    )
    blocked_by_position = _terminalize_stale_pending_entry_orders(
        orders,
        live_position_symbols={"AAAUSDT"},
        live_open_entry_order_symbols=set(),
        now_ms=now_ms,
    )
    blocked_by_open_order = _terminalize_stale_pending_entry_orders(
        orders,
        live_position_symbols=set(),
        live_open_entry_order_symbols={"AAAUSDT"},
        now_ms=now_ms,
    )

    assert [row["order_link_id"] for row in updates] == ["lm-en-stale-flat"]
    assert updates[0]["status"] == "expired_unconfirmed"
    assert "flat Bybit position and no open order" in updates[0]["error"]
    assert blocked_by_position == []
    assert blocked_by_open_order == []


def test_pending_exit_fill_reconciles_to_closed_trade() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-ex-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Buy",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": True,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 0.0,
                "notional_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "exit_reason": "time_exit",
                "exit_trigger_ts_ms": 1_700_000_061_000,
                "target_qty": "1",
                "filled_qty": "",
            }
        ]
    )
    trades_df = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 99.0,
                "notional_usdt": 99.0,
                "equity_usdt": 990.0,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-ex-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        trades_df,
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["status"] == "closed"
    assert trades[0]["exit_reason"] == "time_exit"
    assert trades[0]["exit_trigger_ts_ms"] == 1_700_000_061_000
    assert trades[0]["exit_price"] == 100.5
    # Realized PnL fields must land on close, not depend on the orphan reconciler.
    # short 99→100.5 → gross = (99-100.5)/99 ≈ -0.01515; net = gross * (99/990) = gross * 0.1
    assert trades[0]["gross_trade_return"] == pytest.approx(-1.5 / 99.0)
    assert trades[0]["net_return"] == pytest.approx((-1.5 / 99.0) * 0.1)
    # Schema completeness: venue exec_time + fee fields must always land on
    # close so the demo↔Bybit reconciliation can close the PnL triangle. Even
    # when the FakeRiskClient doesn't surface execTime/execFee, the trade row
    # must carry the *keys* (zero values OK) so downstream consumers don't
    # NameError on a missing column.
    assert "exit_exec_time_ms" in trades[0]
    assert "exit_fee_usdt" in trades[0]
    assert order_updates[0]["status"] == "filled"
    assert order_updates[0]["notional_usdt"] == 100.5
    # Order ledger must mirror trade ledger: fee + exec_time per order row.
    assert "fee_usdt" in order_updates[0]
    assert "exec_time_ms" in order_updates[0]


def test_pending_exit_partial_fill_reduces_open_trade_qty() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-ex-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Buy",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": True,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 0.0,
                "notional_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "exit_reason": "time_exit",
                "target_qty": "1",
                "filled_qty": "",
            }
        ]
    )
    trades_df = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 99.0,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-ex-",), fill_qty="0.4")

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        trades_df,
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["status"] == "open"
    assert trades[0]["qty"] == "0.6"
    assert trades[0]["partial_exit_reason"] == "time_exit"
    assert order_updates[0]["status"] == "partial"
    assert order_updates[0]["filled_qty"] == "0.4"


def test_pending_entry_additional_fill_updates_open_trade_qty() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 40.0,
                "target_notional_pct_equity": 0.2,
                "entry_leverage": 2.0,
                "initial_margin_usdt": 20.0,
                "status": "partial",
                "filled_qty": "0.4",
            }
        ]
    )
    trades_df = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "0.4",
                "entry_price": 100.0,
                "notional_usdt": 40.0,
                "equity_usdt": 10_000.0,
                "entry_leverage": 2.0,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",), fill_qty="0.7")

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        trades_df,
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["qty"] == "0.7"
    assert trades[0]["entry_price"] == 100.5
    assert trades[0]["notional_usdt"] == pytest.approx(70.35)
    assert order_updates[0]["status"] == "partial"
    assert order_updates[0]["filled_qty"] == "0.7"


def test_risk_exit_does_not_close_until_submitted_fill_confirmed() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(),
        risk=EventRiskCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={},
    )

    assert rows == []
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["notional_usdt"] == 0.0


def test_risk_exit_failure_records_auditable_order_context() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fail_order_symbols={"AAAUSDT"}),
        risk=EventRiskCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={},
    )

    assert rows == []
    assert orders[0]["status"] == "failed"
    assert orders[0]["trade_id"] == "t1"
    assert orders[0]["exit_reason"] == "stop_loss"
    assert orders[0]["exit_trigger_ts_ms"] == 1_700_000_000_000
    assert orders[0]["target_qty"] == "1"
    assert orders[0]["filled_qty"] == ""
    assert orders[0]["avg_price"] == 113.0
    assert "order rejected" in orders[0]["error"]


def test_risk_exit_partial_fill_reduces_trade_qty_immediately() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 100.0,
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-rx-",), fill_qty="0.4"),
        risk=EventRiskCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={},
    )

    assert rows[0]["status"] == "open"
    assert rows[0]["qty"] == "0.6"
    assert rows[0]["notional_usdt"] == 60.0
    assert rows[0]["partial_exit_reason"] == "stop_loss"
    assert rows[0]["partial_exit_qty"] == "0.4"
    assert orders[0]["status"] == "partial"
    assert orders[0]["target_qty"] == "1"
    assert orders[0]["filled_qty"] == "0.4"


def test_limit_chase_partial_fills_keep_child_order_quantities() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 100.0,
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-lc-",), fill_qty="0.4"),
        risk=EventRiskCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            exit_order_mode="limit_chase",
            limit_chase_attempts=2,
            limit_chase_wait_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={"AAAUSDT": 0.1},
    )

    assert rows[0]["status"] == "open"
    assert rows[0]["qty"] == "0.2"
    assert [order["order_type"] for order in orders] == ["Limit", "Limit", "Market"]
    assert [order["status"] for order in orders] == ["partial", "partial", "fallback_market"]
    assert [order["target_qty"] for order in orders] == ["1", "0.6", "0.2"]
    assert [order["filled_qty"] for order in orders] == ["0.4", "0.4", ""]
    assert orders[0]["notional_usdt"] == pytest.approx(40.2)
    assert orders[1]["notional_usdt"] == pytest.approx(40.2)


def test_risk_exit_records_filled_order_after_confirmed_fill() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 100.0,
                "equity_usdt": 1_000.0,
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-rx-",)),
        risk=EventRiskCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={},
    )

    assert rows[0]["status"] == "closed"
    # Realized PnL fields must land on close from the risk-exit path too.
    # FakeRiskClient fills at 100.5 (its default), so short 100→100.5 →
    # gross = (100-100.5)/100 = -0.005; net = gross * (100/1000) = -0.0005
    assert rows[0]["gross_trade_return"] == pytest.approx(-0.005)
    assert rows[0]["net_return"] == pytest.approx(-0.0005)
    # Schema completeness on the risk-engine exit path: same field set as the
    # pending-fill reconciler and cycle-exit paths so all 3 close-paths write
    # a uniform shape.
    assert "exit_exec_time_ms" in rows[0]
    assert "exit_fee_usdt" in rows[0]
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled_qty"] == "1"
    assert "fee_usdt" in orders[0]
    assert "exec_time_ms" in orders[0]


def test_run_event_risk_cycle_dry_run_closes_crossed_stop(tmp_path) -> None:
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
                    "planned_exit_ts_ms": 1_700_100_000_000,
                }
            ]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    client = FakeRiskClient(
        positions=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "avgPrice": "100",
                "markPrice": "113",
                "positionValue": "113",
                "unrealisedPnl": "-13",
                "stopLoss": "112",
                "takeProfit": "80",
            }
        ]
    )

    payload = run_event_risk_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventRiskCycleConfig(record_dry_run=True, repair_stops=False),
        private_client=client,
        now_ms=1_700_000_060_000,
    )

    trades = read_dataset(tmp_path, "event_demo_trades")
    assert payload["cycle"]["exits_executed"] == 1
    assert payload["exits"][0]["status"] == "closed"
    assert trades.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert (tmp_path / "reports" / "event-risk" / "latest_event_risk_cycle.md").exists()


def test_reconcile_open_trades_keeps_matching_side_position() -> None:
    """Position exists on the SAME side as the open trade → keep the trade."""
    open_trades = pl.DataFrame([_open_trade_row(side="short")], infer_schema_length=None)
    raw_positions = [{"symbol": "AAAUSDT", "size": "1.0", "side": "Sell"}]

    kept, updates, error = _reconcile_open_trades(
        open_trades,
        trading_client=_ClosedPnlClient(),
        demo=EventDemoCycleConfig(submit_orders=True),
        now_ms=1_700_000_100_000,
        raw_positions=raw_positions,
    )

    assert error == ""
    assert updates == []
    assert kept.height == 1
    assert str(kept.row(0, named=True)["status"]) == "open"


_SHORT_CLOSURE = {
    "symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "95.0",
    "closedSize": "1", "execFee": "0.1", "orderId": "x-1", "createdTime": "1700000050000",
}


def test_reconcile_open_trades_closes_when_position_vanished_with_evidence() -> None:
    """Position gone from Bybit AND a closed-PnL record proves it closed →
    orphan-close (the fail-closed invariant's positive-evidence path)."""
    open_trades = pl.DataFrame([_open_trade_row(side="short")], infer_schema_length=None)

    kept, updates, error = _reconcile_open_trades(
        open_trades,
        trading_client=_ClosedPnlClient(records=[_SHORT_CLOSURE]),
        demo=EventDemoCycleConfig(submit_orders=True),
        now_ms=1_700_000_100_000,
        raw_positions=[],
    )

    assert error == ""
    assert kept.is_empty()
    assert len(updates) == 1
    assert updates[0]["status"] == "closed"
    assert updates[0]["exit_reason"] == "bybit_position_missing"


def test_reconcile_open_trades_keeps_open_when_no_closure_evidence() -> None:
    """FAIL-CLOSED invariant: position absent but NO closure record → the trade
    stays OPEN, not orphan-closed. A transient/empty positions read must never
    wipe a possibly-live position from the ledger (the C1 class)."""
    open_trades = pl.DataFrame([_open_trade_row(side="short")], infer_schema_length=None)

    kept, updates, error = _reconcile_open_trades(
        open_trades,
        trading_client=_ClosedPnlClient(),  # no records -> no evidence
        demo=EventDemoCycleConfig(submit_orders=True),
        now_ms=1_700_000_100_000,
        raw_positions=[],
    )

    assert error == ""
    assert updates == []
    assert kept.height == 1
    assert str(kept.row(0, named=True)["status"]) == "open"


def test_reconcile_open_trades_legacy_closes_on_absence_when_evidence_not_required() -> None:
    """The fail-closed policy is a knob: orphan_close_require_evidence=False
    restores the legacy close-on-absence (zero-PnL) behavior."""
    open_trades = pl.DataFrame([_open_trade_row(side="short")], infer_schema_length=None)

    kept, updates, error = _reconcile_open_trades(
        open_trades,
        trading_client=_ClosedPnlClient(),  # no evidence
        demo=EventDemoCycleConfig(submit_orders=True, orphan_close_require_evidence=False),
        now_ms=1_700_000_100_000,
        raw_positions=[],
    )

    assert kept.is_empty()
    assert len(updates) == 1
    assert updates[0]["exit_reason"] == "bybit_position_missing"


def test_reconcile_open_trades_closes_when_position_flipped_to_opposite_side() -> None:
    """The audit's CRITICAL bug: a SHORT trade is still open in the ledger, but
    on Bybit the position was closed and a new LONG opened on the same symbol
    (manual flip, second daemon, or stop-loss + re-entry). The old reconciler
    keyed by symbol-only saw size > 0 and kept the stale short. The fix keys
    by (symbol, side) so the short is correctly orphan-closed."""
    open_trades = pl.DataFrame([_open_trade_row(side="short")], infer_schema_length=None)
    raw_positions = [{"symbol": "AAAUSDT", "size": "2.5", "side": "Buy"}]

    kept, updates, error = _reconcile_open_trades(
        open_trades,
        trading_client=_ClosedPnlClient(records=[_SHORT_CLOSURE]),
        demo=EventDemoCycleConfig(submit_orders=True),
        now_ms=1_700_000_100_000,
        raw_positions=raw_positions,
    )

    assert error == ""
    assert kept.is_empty()
    assert len(updates) == 1
    assert updates[0]["status"] == "closed"
    assert updates[0]["exit_reason"] == "bybit_position_missing"


def test_risk_reconciler_no_op_on_position_snapshot_error() -> None:
    """A failed get_positions must NOT false-positive orphan-close every trade.

    Without the position_error guard, a transient REST error makes
    position_by_symbol={}, which the reconciler would interpret as "every open
    trade has vanished" and close all of them — destroying the ledger on a
    single API hiccup.
    """
    open_trades = pl.DataFrame([_open_trade_row()], infer_schema_length=None)

    kept, updates = _risk_reconcile_missing_positions(
        open_trades,
        position_by_symbol={},
        now_ms=1_700_000_100_000,
        enabled=True,
        position_error="positions unavailable",
        trading_client=_ClosedPnlClient(),
    )

    assert updates == []
    assert kept.height == 1
    assert str(kept.row(0, named=True)["status"]) == "open"


def test_risk_reconciler_closes_orphan_when_position_missing() -> None:
    """Legitimate orphan close (no position_error, symbol gone from venue)."""
    open_trades = pl.DataFrame([_open_trade_row()], infer_schema_length=None)

    kept, updates = _risk_reconcile_missing_positions(
        open_trades,
        position_by_symbol={},
        now_ms=1_700_000_100_000,
        enabled=True,
        position_error="",
        trading_client=None,
    )

    assert kept.is_empty()
    assert len(updates) == 1
    assert updates[0]["status"] == "closed"
    assert updates[0]["exit_reason"] == "bybit_position_missing"


def test_orphan_close_pnl_backfill_pulls_exit_price_and_return() -> None:
    """Happy path: get_closed_pnl returns a matching record → backfill populates
    exit_price, gross_trade_return, net_return, exit_ts_ms, exit_order_id."""
    trade = _open_trade_row(side="short", entry_price=100.0, notional_usdt=1_000.0, equity_usdt=10_000.0)
    client = _ClosedPnlClient(
        records=[
            {
                "symbol": "AAAUSDT",
                "side": "Buy",  # close side for a short is Buy
                "avgEntryPrice": "100.0",
                "avgExitPrice": "95.0",
                "closedPnl": "5.0",
                "orderId": "exit-order-1",
                "createdTime": "1700000050000",
            }
        ]
    )

    backfill = _orphan_close_pnl_backfill(trade, now_ms=1_700_000_100_000, trading_client=client)

    assert backfill["exit_price"] == 95.0
    # short return = (100 - 95) / 100 = 0.05
    assert backfill["gross_trade_return"] == pytest.approx(0.05)
    # net = gross * notional_weight = 0.05 * (1000 / 10000) = 0.005
    assert backfill["net_return"] == pytest.approx(0.005)
    assert backfill["exit_ts_ms"] == 1_700_000_050_000
    assert backfill["closed_at_ms"] == 1_700_000_050_000
    assert backfill["exit_order_id"] == "exit-order-1"
    assert backfill["submit_mode"] == "orphan_reconciled"


def test_orphan_close_pnl_backfill_aggregates_multi_leg_close() -> None:
    """M8: a position closed via several reduce-only legs must sum execFee and
    qty-weight the exit price across legs, not price the close off one leg."""
    trade = _open_trade_row(
        side="short", entry_price=100.0, entry_ts_ms=1_700_000_000_000,
        notional_usdt=1_000.0, equity_usdt=10_000.0,
    )
    client = _ClosedPnlClient(
        records=[
            {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "96.0", "closedSize": "3",
             "execFee": "0.3", "orderId": "leg-1", "createdTime": "1700000040000"},
            {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "94.0", "closedSize": "1",
             "execFee": "0.1", "orderId": "leg-2", "createdTime": "1700000050000"},
        ]
    )

    backfill = _orphan_close_pnl_backfill(trade, now_ms=1_700_000_100_000, trading_client=client)

    # qty-weighted exit = (3*96 + 1*94) / 4 = 95.5
    assert backfill["exit_price"] == pytest.approx(95.5)
    # execFee summed across both legs (NOT just one leg's 0.1).
    assert backfill["exit_fee_usdt"] == pytest.approx(0.4)
    # Close completes at the last leg's venue time / order.
    assert backfill["closed_at_ms"] == 1_700_000_050_000
    assert backfill["exit_order_id"] == "leg-2"
    assert backfill["orphan_close_legs"] == 2
    # short return on the blended 95.5 = (100 - 95.5) / 100 = 0.045
    assert backfill["gross_trade_return"] == pytest.approx(0.045)


def test_orphan_close_pnl_backfill_returns_empty_when_endpoint_missing() -> None:
    """Falls back silently when the client doesn't expose get_closed_pnl."""

    class _NoEndpoint:
        pass

    trade = _open_trade_row()
    assert _orphan_close_pnl_backfill(trade, now_ms=1_700_000_100_000, trading_client=_NoEndpoint()) == {}


def test_orphan_close_pnl_backfill_returns_empty_on_api_failure() -> None:
    """An API failure must not stop the orphan close — it just falls back to zero-PnL."""
    trade = _open_trade_row()
    client = _ClosedPnlClient(raise_on_call=True)

    assert _orphan_close_pnl_backfill(trade, now_ms=1_700_000_100_000, trading_client=client) == {}


def test_orphan_close_pnl_backfill_skips_records_before_entry() -> None:
    """A closed-PnL record older than entry_ts_ms can't be our close — skip it."""
    trade = _open_trade_row(side="short", entry_ts_ms=1_700_000_000_000)
    client = _ClosedPnlClient(
        records=[
            {
                "symbol": "AAAUSDT",
                "side": "Buy",
                "avgEntryPrice": "90.0",
                "avgExitPrice": "80.0",
                "orderId": "older-close",
                "createdTime": "1_699_999_000_000",
            }
        ]
    )

    assert _orphan_close_pnl_backfill(trade, now_ms=1_700_000_100_000, trading_client=client) == {}


def test_orphan_close_pnl_backfill_skips_wrong_side() -> None:
    """A Sell-side close can't belong to our short trade (a short is closed by Buy)."""
    trade = _open_trade_row(side="short")
    client = _ClosedPnlClient(
        records=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "avgEntryPrice": "100.0",
                "avgExitPrice": "95.0",
                "orderId": "wrong-side",
                "createdTime": "1_700_000_050_000",
            }
        ]
    )

    assert _orphan_close_pnl_backfill(trade, now_ms=1_700_000_100_000, trading_client=client) == {}


def test_risk_reconciler_backfills_pnl_when_trading_client_provided() -> None:
    """End-to-end: reconciler closes the orphan AND fills in exit_price / returns."""
    open_trades = pl.DataFrame([_open_trade_row()], infer_schema_length=None)
    client = _ClosedPnlClient(
        records=[
            {
                "symbol": "AAAUSDT",
                "side": "Buy",
                "avgEntryPrice": "100.0",
                "avgExitPrice": "97.0",
                "orderId": "exit-1",
                "createdTime": "1_700_000_060_000",
            }
        ]
    )

    kept, updates = _risk_reconcile_missing_positions(
        open_trades,
        position_by_symbol={},
        now_ms=1_700_000_100_000,
        enabled=True,
        position_error="",
        trading_client=client,
    )

    assert kept.is_empty()
    assert len(updates) == 1
    update = updates[0]
    assert update["status"] == "closed"
    assert update["exit_reason"] == "bybit_position_missing"
    assert update["exit_price"] == 97.0
    assert update["gross_trade_return"] == pytest.approx(0.03)
    assert update["submit_mode"] == "orphan_reconciled"
    assert update["exit_order_id"] == "exit-1"


def test_execute_exits_writes_preflight_before_place_order() -> None:
    """Preflight callback must be invoked BEFORE place_order so a crash between
    the two still leaves the order_link_id discoverable in parquet."""
    all_trades = pl.DataFrame([_open_trade_row()], infer_schema_length=None)
    preflight_rows: list[dict[str, Any]] = []
    client = FakeRiskClient()
    # Wrap place_order to record the call order vs preflight invocations.
    call_log: list[str] = []
    orig_place_order = client.place_order

    def _logged_place(**params: object) -> dict[str, str]:
        call_log.append("place_order")
        return orig_place_order(**params)

    client.place_order = _logged_place  # type: ignore[method-assign]

    def _record(row: dict[str, Any]) -> None:
        call_log.append("preflight")
        preflight_rows.append(row)

    _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
        record_preflight=_record,
    )

    assert call_log[:2] == ["preflight", "place_order"], call_log
    assert len(preflight_rows) == 1
    pre = preflight_rows[0]
    assert pre["submit_mode"] == "preflight"
    assert pre["status"] == "submitted"
    assert pre["reduce_only"] is True
    assert pre["side"] == "Buy"  # short closes with Buy
    assert pre["trade_id"] == "t1"
    assert pre["exit_reason"] == "max_hold"


def test_execute_exits_skips_preflight_on_dry_run() -> None:
    """Dry-run (submit_orders=False) must not call the preflight callback —
    nothing is being submitted to the venue, so there's no crash-window to guard."""
    all_trades = pl.DataFrame([_open_trade_row()], infer_schema_length=None)
    preflight_rows: list[dict[str, Any]] = []

    def _record(row: dict[str, Any]) -> None:
        preflight_rows.append(row)

    _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=None,
        demo=EventDemoCycleConfig(submit_orders=False),
        now_ms=1_700_000_060_000,
        record_preflight=_record,
    )

    assert preflight_rows == []


def test_execute_exits_keeps_trade_open_when_no_exit_price_resolvable() -> None:
    """BUG-5: a paper/dry-run max_hold on a fully-delisted coin has no
    planned_exit_price (gone from BOTH the universe and get_tickers). The exit must
    NOT close the trade at a fabricated exit_price=0 / 0% return — it must stay open
    for a later retry (or, on the submit path, the orphan reconciler's settlement)."""
    all_trades = pl.DataFrame([_open_trade_row()], infer_schema_length=None)

    rows, _orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": None,  # delisted: no resolvable price
            }
        ],
        all_trades,
        trading_client=None,
        demo=EventDemoCycleConfig(submit_orders=False),
        now_ms=1_700_000_060_000,
    )
    # No closed row booked — the trade stays open (no fabricated 0% return).
    assert rows == []

    # Control: a resolvable price DOES close it normally.
    rows_ok, _ = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=None,
        demo=EventDemoCycleConfig(submit_orders=False),
        now_ms=1_700_000_060_000,
    )
    assert len(rows_ok) == 1
    assert rows_ok[0]["status"] == "closed"
    assert rows_ok[0]["exit_price"] == 99.0


def test_preflight_exit_order_row_uses_pending_status() -> None:
    """The preflight row's status must be in PENDING_ORDER_STATUSES so the
    next-cycle _reconcile_pending_order_fills picks it up if our cycle crashed."""
    from liquidity_migration.event_demo import PENDING_ORDER_STATUSES

    row = _preflight_exit_order_row(
        exit_link="lm-ex-AAA-abc",
        now_ms=1_700_000_060_000,
        trade_id="t1",
        symbol="AAAUSDT",
        bybit_side="Buy",
        order_type="Market",
        qty="1",
        exit_plan={"exit_reason": "max_hold", "exit_trigger_ts_ms": 1_700_000_000_000},
    )

    assert row["status"] in PENDING_ORDER_STATUSES
    assert row["submit_mode"] == "preflight"
    assert row["reduce_only"] is True


def test_submit_reduce_only_exit_writes_preflight_before_place_order_market() -> None:
    """Market-mode reduce-only exit: record_preflight must fire before place_order."""
    from liquidity_migration.event_demo import _submit_reduce_only_exit, EventRiskCycleConfig

    call_log: list[str] = []
    client = FakeRiskClient()
    orig_place_order = client.place_order

    def _logged_place(**params: object) -> dict[str, str]:
        call_log.append("place_order")
        return orig_place_order(**params)

    client.place_order = _logged_place  # type: ignore[method-assign]

    preflight_rows: list[dict[str, Any]] = []

    def _record(row: dict[str, Any]) -> None:
        call_log.append("preflight")
        preflight_rows.append(row)

    _submit_reduce_only_exit(
        symbol="AAAUSDT",
        bybit_side="Buy",
        qty="1",
        trading_client=client,
        risk=EventRiskCycleConfig(submit_orders=True, exit_order_mode="market"),
        now_ms=1_700_000_060_000,
        reference_price=100.0,
        tick_size=0.1,
        record_preflight=_record,
    )

    assert call_log[:2] == ["preflight", "place_order"], call_log
    assert len(preflight_rows) == 1
    pre = preflight_rows[0]
    assert pre["submit_mode"] == "preflight"
    assert pre["status"] == "submitted"
    assert pre["order_type"] == "Market"
    assert pre["side"] == "Buy"


def test_submit_reduce_only_exit_skips_preflight_on_dry_run() -> None:
    """When submit_orders=False the helper returns a dry-run row directly;
    no preflight callback should fire because nothing reaches the venue."""
    from liquidity_migration.event_demo import _submit_reduce_only_exit, EventRiskCycleConfig

    preflight_rows: list[dict[str, Any]] = []
    _submit_reduce_only_exit(
        symbol="AAAUSDT",
        bybit_side="Buy",
        qty="1",
        trading_client=None,
        risk=EventRiskCycleConfig(submit_orders=False),
        now_ms=1_700_000_060_000,
        reference_price=100.0,
        tick_size=0.1,
        record_preflight=lambda row: preflight_rows.append(row),
    )

    assert preflight_rows == []


def test_execute_exits_splits_close_when_position_exceeds_max_mkt_qty() -> None:
    """SUPER-shape position close: target qty exceeds Bybit's maxMktOrderQty
    so the exit must split into N reduce-only sub-orders sharing the base
    exit link with -s0, -s1 suffixes — mirror of the entry-side split."""
    # Open trade for a 37500-contract short with max_market_order_qty=20000.
    # A single reduce-only place_order at 37500 would be rejected; the split
    # must turn it into 2 ~18750-contract sub-orders.
    trade_row = _open_trade_row(
        symbol="SUPERUSDT",
        qty="37500",
        entry_price=2.0,
        notional_usdt=75_000.0,
        qty_step=1.0,
        tick_size=0.0001,
        max_market_order_qty=20000.0,
    )
    all_trades = pl.DataFrame([trade_row], infer_schema_length=None)
    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("lm-ex-",),
        fill_qty="18750",
        fill_price="1.95",
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "SUPERUSDT",
                "side": "short",
                "qty": "37500",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 1.95,
            }
        ],
        all_trades,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    # 2 reduce-only sub-orders submitted, 1 trade row closed.
    assert len(orders) == 2, f"expected 2 sub-order rows, got {len(orders)}"
    assert len(rows) == 1, f"expected 1 closed trade row, got {len(rows)}"
    # Sub-links share a base with -s0 / -s1 suffixes.
    base_links = {o["order_link_id"].rsplit("-s", 1)[0] for o in orders}
    assert len(base_links) == 1, base_links
    suffixes = sorted(o["order_link_id"].rsplit("-s", 1)[1] for o in orders)
    assert suffixes == ["0", "1"], suffixes
    # All sub-orders are reduce-only and under the cap.
    for o in orders:
        assert o["reduce_only"] is True
        assert float(o["qty"]) <= 20000.0
    # Aggregated close: trade row reflects full fill at the (uniform) avg
    # price the FakeRiskClient stubs out.
    assert rows[0]["status"] == "closed"
    assert float(rows[0]["exit_price"]) == pytest.approx(1.95)


def test_execute_exits_no_split_when_cap_does_not_bind() -> None:
    """Position qty within the venue cap: a single reduce-only order is
    submitted with the original (un-suffixed) exit_link, preserving the
    pre-2026-05-27 behaviour for trades not affected by the split."""
    trade_row = _open_trade_row(
        symbol="AAAUSDT",
        qty="50",
        qty_step=0.1,
        max_market_order_qty=1000.0,  # well above target
    )
    all_trades = pl.DataFrame([trade_row], infer_schema_length=None)
    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("lm-ex-",),
        fill_qty="50",
        fill_price="100",
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "50",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert len(orders) == 1
    assert "-s" not in orders[0]["order_link_id"].rsplit("-", 1)[-1]
    assert len(rows) == 1
    assert rows[0]["status"] == "closed"


def test_execute_exits_legacy_trade_row_without_max_qty_does_not_split() -> None:
    """Legacy trade rows persisted before max_market_order_qty was tracked
    must still close as a single reduce-only order (no crash, no split)."""
    trade_row = _open_trade_row(symbol="AAAUSDT", qty="50")  # no qty_step or max_qty
    all_trades = pl.DataFrame([trade_row], infer_schema_length=None)
    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("lm-ex-",),
        fill_qty="50",
        fill_price="100",
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "50",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert len(orders) == 1
    assert len(rows) == 1


def test_submit_reduce_only_exit_market_splits_at_max_qty() -> None:
    """The wsrisk-cycle reduce-only exit must split a SUPER-shape close into
    N market sub-orders, aggregating fills into a single exec_summary."""
    from liquidity_migration.event_demo import _submit_reduce_only_exit, EventRiskCycleConfig

    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("lm-rx-",),
        fill_qty="18750",
        fill_price="1.95",
    )
    submit = _submit_reduce_only_exit(
        symbol="SUPERUSDT",
        bybit_side="Buy",
        qty="37500",
        trading_client=client,
        risk=EventRiskCycleConfig(submit_orders=True, exit_order_mode="market"),
        now_ms=1_700_000_060_000,
        reference_price=1.95,
        tick_size=0.0001,
        max_qty_per_order=20000.0,
        qty_step=1.0,
    )

    # 2 sub-orders submitted, exec_summary aggregates 18750 + 18750 = 37500.
    assert len(submit["order_rows"]) == 2
    for row in submit["order_rows"]:
        assert row["reduce_only"] is True
        assert float(row["qty"]) <= 20000.0
    assert float(submit["exec_summary"]["qty"]) == pytest.approx(37500.0)
    assert float(submit["exec_summary"]["avg_price"]) == pytest.approx(1.95)

