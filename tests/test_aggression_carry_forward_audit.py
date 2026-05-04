from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from aggression_carry.forward_audit import run_forward_demo_audit
from aggression_carry.storage import write_dataset


NOW = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)


def test_forward_demo_audit_joins_paper_and_demo_ledgers_by_sleeve(tmp_path: Path) -> None:
    data_root = tmp_path / "forward-paper"
    sleeve_root = data_root / "forward_sleeves" / "core_31_150"
    _write_paper_trades(
        sleeve_root,
        [
            _paper_trade("paper-1", status="closed", exit_price=98.0, weighted_net_return=0.003),
            _paper_trade("paper-2", status="open", symbol="ETHUSDT", weighted_net_return=-0.001),
        ],
    )
    _write_demo_orders(
        sleeve_root,
        [
            _demo_order(
                "core-entry-1",
                paper_trade_id="paper-1",
                action="entry",
                side="Sell",
                price="100.2",
                filled_qty=0.05,
                filled_value=4.975,
            ),
            _demo_order(
                "core-exit-1",
                paper_trade_id="paper-1",
                action="exit",
                side="Buy",
                price="",
                filled_qty=0.05,
                filled_value=4.94,
                reduce_only=True,
            ),
        ],
    )

    payload = run_forward_demo_audit(data_root)

    trades = pl.read_csv(data_root / "reports" / "forward_demo_audit_trades.csv")
    daily = pl.read_csv(data_root / "reports" / "forward_demo_audit_daily.csv")
    filled = trades.filter(pl.col("paper_trade_id") == "paper-1").row(0, named=True)
    missing = trades.filter(pl.col("paper_trade_id") == "paper-2").row(0, named=True)

    assert payload["rows"]["trade_audit_rows"] == 2
    assert payload["summary"]["demo_entries_filled"] == 1
    assert payload["summary"]["demo_exits_filled"] == 1
    assert filled["sleeve"] == "core_31_150"
    assert filled["entry_fill_status"] == "filled"
    assert filled["exit_fill_status"] == "filled"
    assert abs(filled["entry_slippage_bps"] - 50.0) < 1e-9
    assert abs(filled["exit_slippage_bps"] - 81.6326530612) < 1e-6
    assert abs(filled["demo_realized_pnl_usdt"] - 0.035) < 1e-9
    assert missing["missed_reason"] == "entry_order_missing"
    assert daily.row(0, named=True)["paper_weighted_net_return"] == 0.002
    assert (data_root / "reports" / "forward_demo_audit_report.md").exists()


def _write_paper_trades(data_root: Path, rows: list[dict]) -> None:
    write_dataset(
        pl.DataFrame(rows, infer_schema_length=None),
        data_root,
        "forward_paper_trades",
        partition_by=("date", "symbol"),
        append=False,
    )


def _write_demo_orders(data_root: Path, rows: list[dict]) -> None:
    write_dataset(
        pl.DataFrame(rows, infer_schema_length=None),
        data_root,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )


def _paper_trade(
    trade_id: str,
    *,
    status: str,
    symbol: str = "BTCUSDT",
    exit_price: float | None = None,
    weighted_net_return: float,
) -> dict:
    return {
        "trade_id": trade_id,
        "basket_id": "basket-1",
        "status": status,
        "symbol": symbol,
        "side": "short",
        "date": "2026-01-15",
        "entry_ts_ms": int(NOW.timestamp() * 1000),
        "entry_time": NOW.isoformat(),
        "entry_price": 100.0,
        "exit_ts_ms": int(datetime(2026, 1, 16, 1, 16, tzinfo=UTC).timestamp() * 1000) if exit_price else None,
        "exit_time": "2026-01-16T01:16:00+00:00" if exit_price else None,
        "exit_price": exit_price,
        "exit_reason": "max_hold" if status == "closed" else "open",
        "mark_price": 99.5,
        "weight": 0.2,
        "net_return": weighted_net_return / 0.2,
        "weighted_net_return": weighted_net_return,
        "actual_notional": 2_000.0,
        "target_notional": 2_000.0,
    }


def _demo_order(
    order_link_id: str,
    *,
    paper_trade_id: str,
    action: str,
    side: str,
    price: str,
    filled_qty: float,
    filled_value: float,
    reduce_only: bool = False,
) -> dict:
    return {
        "order_link_id": order_link_id,
        "order_id": f"order-{order_link_id}",
        "paper_trade_id": paper_trade_id,
        "basket_id": "basket-1",
        "date": "2026-01-15",
        "action": action,
        "status": "accepted",
        "symbol": "BTCUSDT",
        "side": side,
        "order_type": "Market" if reduce_only else "Limit",
        "time_in_force": "IOC" if reduce_only else "PostOnly",
        "qty": "0.05",
        "price": price,
        "reduce_only": reduce_only,
        "estimated_notional": filled_value,
        "max_order_notional": 10.0,
        "created_ts_ms": int(NOW.timestamp() * 1000),
        "created_time": NOW.isoformat(),
        "filled_qty": filled_qty,
        "filled_value": filled_value,
        "reconciled_status": "filled",
        "error": "",
        "reconcile_error": "",
    }
