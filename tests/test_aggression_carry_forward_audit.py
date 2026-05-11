from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from aggression_carry.forward_audit import run_forward_demo_audit
from aggression_carry.storage import write_dataset


NOW = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)


def test_forward_demo_audit_joins_paper_and_demo_ledgers_by_sleeve(tmp_path: Path) -> None:
    data_root = tmp_path / "forward-paper"
    sleeve_root = data_root / "forward_sleeves" / "stage4_selected"
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
                "stg4-entry-1",
                paper_trade_id="paper-1",
                action="entry",
                side="Sell",
                price="100.2",
                filled_qty=0.05,
                filled_value=4.975,
            ),
            _demo_order(
                "stg4-exit-1",
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
    assert filled["sleeve"] == "stage4_selected"
    assert filled["entry_fill_status"] == "filled"
    assert filled["exit_fill_status"] == "filled"
    assert abs(filled["entry_slippage_bps"] - 50.0) < 1e-9
    assert abs(filled["exit_slippage_bps"] - 81.6326530612) < 1e-6
    assert abs(filled["demo_realized_pnl_usdt"] - 0.035) < 1e-9
    assert missing["missed_reason"] == "entry_order_missing"
    assert daily.row(0, named=True)["paper_weighted_net_return"] == 0.002
    assert (data_root / "reports" / "forward_demo_audit_report.md").exists()


def test_forward_demo_audit_aggregates_multiple_exit_fills(tmp_path: Path) -> None:
    data_root = tmp_path / "forward-paper"
    sleeve_root = data_root / "forward_sleeves" / "stage4_selected"
    _write_paper_trades(sleeve_root, [_paper_trade("paper-1", status="closed", exit_price=98.0, weighted_net_return=0.003)])
    _write_demo_orders(
        sleeve_root,
        [
            _demo_order(
                "stg4-entry-1",
                paper_trade_id="paper-1",
                action="entry",
                side="Sell",
                price="100.0",
                filled_qty=0.05,
                filled_value=5.0,
            ),
            _demo_order(
                "stg4-exit-1",
                paper_trade_id="paper-1",
                action="exit",
                side="Buy",
                price="",
                filled_qty=0.02,
                filled_value=1.96,
                reduce_only=True,
            ),
            _demo_order(
                "stg4-exit-2",
                paper_trade_id="paper-1",
                action="exit",
                side="Buy",
                price="",
                filled_qty=0.03,
                filled_value=2.91,
                reduce_only=True,
            ),
        ],
    )

    payload = run_forward_demo_audit(data_root)
    trades = pl.read_csv(data_root / "reports" / "forward_demo_audit_trades.csv")
    row = trades.row(0, named=True)

    assert payload["summary"]["demo_exits_filled"] == 1
    assert payload["summary"]["demo_exit_orders"] == 2
    assert payload["summary"]["demo_exit_fill_order_events"] == 2
    assert row["exit_order_link_id"] == "2 exit orders"
    assert row["exit_order_count"] == 2
    assert row["exit_fill_order_count"] == 2
    assert row["exit_filled_qty"] == 0.05
    assert row["exit_filled_value"] == 4.87
    assert abs(row["exit_avg_fill_price"] - 97.4) < 1e-9
    assert abs(row["demo_realized_pnl_usdt"] - 0.13) < 1e-9


def test_forward_demo_audit_reports_slice_level_demo_state(tmp_path: Path) -> None:
    data_root = tmp_path / "forward-paper"
    sleeve_root = data_root / "forward_sleeves" / "stage4_selected"
    slice_time = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    _write_paper_trades(
        sleeve_root,
        [_paper_trade("paper-1", status="open", weighted_net_return=0.0)],
    )
    _write_paper_slices(
        sleeve_root,
        [_paper_slice("paper-1", 1, slice_time, fill_price=100.0)],
    )
    _write_demo_orders(
        sleeve_root,
        [
            _demo_order(
                "stg4-entry-1",
                paper_trade_id="paper-1",
                action="entry",
                side="Sell",
                price="100.2",
                filled_qty=0.05,
                filled_value=4.975,
                slice_ts_ms=int(slice_time.timestamp() * 1000),
            ),
        ],
    )

    payload = run_forward_demo_audit(data_root)

    slices = pl.read_csv(data_root / "reports" / "forward_demo_audit_slices.csv")
    row = slices.row(0, named=True)
    slice_daily = pl.read_csv(data_root / "reports" / "forward_demo_audit_slice_daily.csv")
    slice_summary = slice_daily.row(0, named=True)
    assert payload["rows"]["slice_audit_rows"] == 1
    assert payload["summary"]["due_paper_slices"] == 1
    assert payload["summary"]["demo_slices_filled"] == 1
    assert payload["summary"]["demo_slices_missing"] == 0
    assert row["expected_slice_time"] == "2026-01-15T22:16:00+00:00"
    assert row["demo_order_link_id"] == "stg4-entry-1"
    assert row["demo_fill_status"] == "filled"
    assert abs(row["entry_slippage_bps"] - 50.0) < 1e-9
    assert slice_summary["due_paper_slices"] == 1
    assert slice_summary["demo_slice_fill_rate"] == 1.0


def test_forward_demo_audit_summarizes_missing_child_slices_even_when_trade_has_a_fill(tmp_path: Path) -> None:
    data_root = tmp_path / "forward-paper"
    sleeve_root = data_root / "forward_sleeves" / "stage4_selected"
    first_slice_time = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    second_slice_time = datetime(2026, 1, 15, 22, 17, tzinfo=UTC)
    _write_paper_trades(
        sleeve_root,
        [_paper_trade("paper-1", status="open", weighted_net_return=0.0)],
    )
    _write_paper_slices(
        sleeve_root,
        [
            _paper_slice("paper-1", 1, first_slice_time, fill_price=100.0),
            _paper_slice("paper-1", 2, second_slice_time, fill_price=101.0),
        ],
    )
    _write_demo_orders(
        sleeve_root,
        [
            _demo_order(
                "stg4-entry-1",
                paper_trade_id="paper-1",
                action="entry",
                side="Sell",
                price="100.2",
                filled_qty=0.05,
                filled_value=4.975,
                slice_ts_ms=int(first_slice_time.timestamp() * 1000),
            ),
        ],
    )

    payload = run_forward_demo_audit(data_root)

    trades = pl.read_csv(data_root / "reports" / "forward_demo_audit_trades.csv")
    slice_daily = pl.read_csv(data_root / "reports" / "forward_demo_audit_slice_daily.csv")
    trade_row = trades.row(0, named=True)
    slice_row = slice_daily.row(0, named=True)

    assert trade_row["entry_fill_status"] == "filled"
    assert trade_row["missed_reason"] in ("", None)
    assert payload["summary"]["due_paper_slices"] == 2
    assert payload["summary"]["demo_slice_orders"] == 1
    assert payload["summary"]["demo_slices_filled"] == 1
    assert payload["summary"]["demo_slices_missing"] == 1
    assert payload["summary"]["demo_slice_fill_rate"] == 0.5
    assert slice_row["due_paper_slices"] == 2
    assert slice_row["demo_slices_missing"] == 1


def test_forward_demo_audit_does_not_count_pending_or_cancelled_paper_slices_as_demo_misses(tmp_path: Path) -> None:
    data_root = tmp_path / "forward-paper"
    sleeve_root = data_root / "forward_sleeves" / "stage4_selected"
    _write_paper_trades(
        sleeve_root,
        [_paper_trade("paper-1", status="open", weighted_net_return=0.0)],
    )
    _write_paper_slices(
        sleeve_root,
        [
            _paper_slice(
                "paper-1",
                1,
                datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
                fill_price=0.0,
                status="pending",
            ),
            _paper_slice(
                "paper-1",
                2,
                datetime(2026, 1, 15, 22, 17, tzinfo=UTC),
                fill_price=0.0,
                status="cancelled_after_exit",
            ),
        ],
    )

    payload = run_forward_demo_audit(data_root)

    assert payload["summary"]["paper_slices"] == 2
    assert payload["summary"]["due_paper_slices"] == 0
    assert payload["summary"]["paper_slices_pending"] == 1
    assert payload["summary"]["paper_slices_cancelled"] == 1
    assert payload["summary"]["demo_slices_missing"] == 0
    assert payload["summary"]["demo_slice_fill_rate"] is None


def test_forward_demo_audit_sends_deduped_telegram_events(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "forward-paper"
    sleeve_root = data_root / "forward_sleeves" / "stage4_selected"
    _write_paper_trades(
        sleeve_root,
        [_paper_trade("paper-1", status="closed", exit_price=98.0, weighted_net_return=0.003)],
    )
    _write_demo_orders(
        sleeve_root,
        [
            _demo_order(
                "stg4-entry-1",
                paper_trade_id="paper-1",
                action="entry",
                side="Sell",
                price="100.2",
                filled_qty=0.05,
                filled_value=4.975,
            ),
            _demo_order(
                "stg4-exit-1",
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
    messages: list[str] = []

    def fake_send(text: str, *, enabled: bool = True, config=None) -> bool:
        del enabled, config
        messages.append(text)
        return True

    monkeypatch.setattr("aggression_carry.forward_audit.send_telegram_message", fake_send)

    audit_now = datetime(2026, 1, 16, 3, 0, tzinfo=UTC)
    first = run_forward_demo_audit(data_root, send_telegram=True, now=audit_now)
    second = run_forward_demo_audit(data_root, send_telegram=True, now=audit_now)

    assert first["telegram"]["sent"] is True
    assert first["telegram"]["events"] == 3
    assert second["telegram"]["reason"] == "no_trade_signal"
    assert len(messages) == 1
    assert messages[0].startswith("MODEL050426 forward audit events")
    assert "Positions entered:" in messages[0]
    assert "Positions exited:" in messages[0]
    assert "End-of-day PnL:" in messages[0]
    assert "entry_order_missing" not in messages[0]


def test_forward_demo_audit_telegram_ignores_pending_and_missing_rows(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "forward-paper"
    sleeve_root = data_root / "forward_sleeves" / "stage4_selected"
    _write_paper_trades(
        sleeve_root,
        [_paper_trade("paper-1", status="open", weighted_net_return=-0.001)],
    )
    messages: list[str] = []
    monkeypatch.setattr(
        "aggression_carry.forward_audit.send_telegram_message",
        lambda text, *, enabled=True, config=None: messages.append(text) or True,
    )

    payload = run_forward_demo_audit(
        data_root,
        send_telegram=True,
        now=datetime(2026, 1, 15, 23, 0, tzinfo=UTC),
    )

    assert payload["telegram"]["reason"] == "no_trade_signal"
    assert messages == []


def test_forward_demo_audit_telegram_alerts_unfilled_exit(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "forward-paper"
    sleeve_root = data_root / "forward_sleeves" / "stage4_selected"
    _write_paper_trades(
        sleeve_root,
        [_paper_trade("paper-1", status="closed", exit_price=98.0, weighted_net_return=0.003)],
    )
    _write_demo_orders(
        sleeve_root,
        [
            _demo_order(
                "stg4-entry-1",
                paper_trade_id="paper-1",
                action="entry",
                side="Sell",
                price="100.0",
                filled_qty=0.05,
                filled_value=5.0,
            ),
            _demo_order(
                "stg4-exit-1",
                paper_trade_id="paper-1",
                action="exit",
                side="Buy",
                price="98.5",
                filled_qty=0.0,
                filled_value=0.0,
                reduce_only=True,
                reconciled_status="cancelled",
            ),
        ],
    )
    messages: list[str] = []
    monkeypatch.setattr(
        "aggression_carry.forward_audit.send_telegram_message",
        lambda text, *, enabled=True, config=None: messages.append(text) or True,
    )

    payload = run_forward_demo_audit(
        data_root,
        send_telegram=True,
        now=datetime(2026, 1, 16, 1, 17, tzinfo=UTC),
    )

    assert payload["telegram"]["sent"] is True
    assert payload["telegram"]["events"] == 2
    assert "Critical errors:" in messages[0]
    assert "exit_cancelled" in messages[0]


def test_forward_demo_audit_skips_telegram_when_empty(tmp_path: Path, monkeypatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(
        "aggression_carry.forward_audit.send_telegram_message",
        lambda text, *, enabled=True, config=None: messages.append(text) or True,
    )

    payload = run_forward_demo_audit(tmp_path / "forward-paper", send_telegram=True)

    assert payload["telegram"]["reason"] == "no_trade_signal"
    assert messages == []


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


def _write_paper_slices(data_root: Path, rows: list[dict]) -> None:
    write_dataset(
        pl.DataFrame(rows, infer_schema_length=None),
        data_root,
        "forward_paper_slices",
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


def _paper_slice(
    trade_id: str,
    slice_index: int,
    scheduled: datetime,
    *,
    fill_price: float,
    status: str = "filled",
) -> dict:
    return {
        "trade_id": trade_id,
        "basket_id": "basket-1",
        "symbol": "BTCUSDT",
        "date": "2026-01-15",
        "side": "short",
        "signal_ts_ms": int(datetime(2026, 1, 15, 22, 0, tzinfo=UTC).timestamp() * 1000),
        "signal_time": "2026-01-15T22:00:00+00:00",
        "paper_status": "open",
        "paper_entry_price": fill_price,
        "paper_exit_price": None,
        "paper_exit_reason": "open",
        "target_notional": 2_000.0,
        "actual_notional": 100.0,
        "entry_twap_minutes": 60,
        "slice_index": slice_index,
        "scheduled_ts_ms": int(scheduled.timestamp() * 1000),
        "scheduled_time": scheduled.isoformat(),
        "fill_ts_ms": int(scheduled.timestamp() * 1000),
        "fill_time": scheduled.isoformat(),
        "status": status,
        "fill_price": fill_price,
        "avg_entry_price": fill_price,
        "stop_price": fill_price * 1.2,
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
    slice_ts_ms: int | None = None,
    reconciled_status: str = "filled",
) -> dict:
    return {
        "order_link_id": order_link_id,
        "order_id": f"order-{order_link_id}",
        "paper_trade_id": paper_trade_id,
        "basket_id": "basket-1",
        "date": "2026-01-15",
        "action": action,
        "slice_index": 1 if slice_ts_ms is not None else 0,
        "slice_ts_ms": slice_ts_ms,
        "slice_time": datetime.fromtimestamp(slice_ts_ms / 1000, tz=UTC).isoformat() if slice_ts_ms is not None else "",
        "paper_slice_status": "filled" if slice_ts_ms is not None else "",
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
        "reconciled_status": reconciled_status,
        "error": "",
        "reconcile_error": "",
    }
