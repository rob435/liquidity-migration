from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from aggression_carry.config import ResearchConfig
from aggression_carry.demo_cycle import DEMO_CYCLE_SLEEVES, DemoCycleConfig, run_bybit_demo_cycle
from aggression_carry.storage import read_dataset, write_dataset


NOW = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)


def test_demo_cycle_dry_run_syncs_all_default_sleeves_without_private_orders(tmp_path: Path) -> None:
    execution = _FakeExecution()

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(max_order_notional=11.0),
        now=NOW,
        forward_runner=_forward_runner_open_trade,
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert payload["rows"]["sleeves"] == 3
    assert payload["summary"]["dry_run"] == 3
    assert payload["summary"]["placed"] == 0
    assert execution.placed == []
    for sleeve in DEMO_CYCLE_SLEEVES:
        sleeve_root = tmp_path / "forward_sleeves" / sleeve
        orders = read_dataset(sleeve_root, "demo_execution_orders")
        assert orders.height == 1
        assert orders.row(0, named=True)["status"] == "dry_run"
        assert (sleeve_root / "reports" / "bybit_demo_sync_report.md").exists()
    assert (tmp_path / "reports" / "bybit_demo_cycle_report.json").exists()
    assert (tmp_path / "reports" / "bybit_demo_cycle_report.md").exists()


def test_demo_cycle_requires_confirmation_before_submit(tmp_path: Path) -> None:
    called = False

    def forward_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return _forward_runner_open_trade(*args, **kwargs)

    try:
        run_bybit_demo_cycle(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            cycle_config=DemoCycleConfig(submit_orders=True, confirmed=False),
            now=NOW,
            forward_runner=forward_runner,
            market_client=_FakeMarket(),
            execution_client=_FakeExecution(),
        )
    except RuntimeError as exc:
        assert "--i-understand-demo-sync" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("demo cycle submission should require confirmation")
    assert called is False


def test_demo_cycle_prefixes_order_link_ids_by_sleeve_on_submit(tmp_path: Path) -> None:
    execution = _FakeExecution()

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(submit_orders=True, confirmed=True, max_order_notional=11.0),
        now=NOW,
        forward_runner=_forward_runner_open_trade,
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    order_link_ids = [order["orderLinkId"] for order in execution.placed]
    assert payload["summary"]["placed"] == 3
    assert len(order_link_ids) == 3
    assert len(set(order_link_ids)) == 3
    assert any(order_link_id.startswith("agcctle") for order_link_id in order_link_ids)
    assert any(order_link_id.startswith("agccoree") for order_link_id in order_link_ids)
    assert any(order_link_id.startswith("agcmicroe") for order_link_id in order_link_ids)


def test_demo_cycle_pause_blocks_entries_but_allows_reduce_only_exits(tmp_path: Path) -> None:
    (tmp_path / "DEMO_PAUSED").write_text("maintenance", encoding="utf-8")
    execution = _FakeExecution(position_size=0.05, position_value=5.0)

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(submit_orders=True, confirmed=True, max_order_notional=11.0),
        now=NOW,
        forward_runner=_forward_runner_open_and_closed_trades,
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert payload["paused"]["paused"] is True
    assert payload["summary"]["new_orders"] == 6
    assert payload["summary"]["accepted"] == 3
    assert payload["summary"]["skipped"] == 3
    assert payload["summary"]["dry_run"] == 0
    assert len(execution.placed) == 3
    assert {order["orderType"] for order in execution.placed} == {"Market"}
    assert {order["reduceOnly"] for order in execution.placed} == {True}
    assert all(order["side"] == "Buy" for order in execution.placed)


def test_demo_cycle_lock_blocks_overlapping_runs(tmp_path: Path) -> None:
    (tmp_path / ".bybit_demo_cycle.lock").write_text("busy", encoding="utf-8")

    try:
        run_bybit_demo_cycle(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            cycle_config=DemoCycleConfig(),
            now=NOW,
            forward_runner=_forward_runner_open_trade,
            market_client=_FakeMarket(),
            execution_client=_FakeExecution(),
        )
    except RuntimeError as exc:
        assert "already running" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("overlapping demo cycle should be blocked by lock")


def test_demo_cycle_outside_window_without_active_state_skips_public_scan_and_sync(tmp_path: Path) -> None:
    forward_called = False
    sync_called = False

    def forward_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal forward_called
        forward_called = True
        return _forward_runner_open_trade(*args, **kwargs)

    def sync_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal sync_called
        sync_called = True
        return {"rows": {"new_orders": 0, "ledger_orders": 0}, "summary": {}}

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(),
        now=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        forward_runner=forward_runner,
        sync_runner=sync_runner,
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )

    assert forward_called is False
    assert sync_called is False
    assert payload["active_window"]["inside"] is False
    assert payload["rows"]["failed_sleeves"] == 0
    assert {row["status"] for row in payload["sleeves"]} == {"inactive_window"}


def _forward_runner_open_trade(data_root: str | Path, **kwargs: Any) -> dict[str, Any]:
    del kwargs
    root = Path(data_root)
    results = []
    for sleeve in DEMO_CYCLE_SLEEVES:
        sleeve_root = root / "forward_sleeves" / sleeve
        _write_forward_trades(sleeve_root, [_paper_trade("paper-shared", status="open")])
        results.append({"sleeve": sleeve, "data_root": str(sleeve_root), "new_trades": 1, "open_trades": 1})
    return {"now": NOW.isoformat(), "rows": {"sleeves": len(results)}, "results": results}


def _forward_runner_open_and_closed_trades(data_root: str | Path, **kwargs: Any) -> dict[str, Any]:
    del kwargs
    root = Path(data_root)
    results = []
    for sleeve in DEMO_CYCLE_SLEEVES:
        sleeve_root = root / "forward_sleeves" / sleeve
        closed_trade_id = f"paper-exit-{sleeve}"
        _write_forward_trades(
            sleeve_root,
            [
                _paper_trade(f"paper-entry-{sleeve}", status="open"),
                _paper_trade(closed_trade_id, status="closed", exit_price=99.0),
            ],
        )
        _write_existing_demo_entry(sleeve_root, closed_trade_id)
        results.append({"sleeve": sleeve, "data_root": str(sleeve_root), "new_trades": 1, "open_trades": 1})
    return {"now": NOW.isoformat(), "rows": {"sleeves": len(results)}, "results": results}


def _write_forward_trades(data_root: Path, rows: list[dict[str, Any]]) -> None:
    write_dataset(
        pl.DataFrame(rows, infer_schema_length=None),
        data_root,
        "forward_paper_trades",
        partition_by=("date", "symbol"),
        append=False,
    )


def _write_existing_demo_entry(data_root: Path, paper_trade_id: str) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": f"existing-{paper_trade_id}",
                    "order_id": "demo-entry",
                    "paper_trade_id": paper_trade_id,
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "status": "placed",
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "order_type": "Limit",
                    "time_in_force": "PostOnly",
                    "qty": "0.05",
                    "price": "101.1",
                    "reduce_only": False,
                    "estimated_notional": 5.055,
                    "max_order_notional": 11.0,
                    "created_ts_ms": int(NOW.timestamp() * 1000),
                    "created_time": NOW.isoformat(),
                }
            ],
            infer_schema_length=None,
        ),
        data_root,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )


def _paper_trade(trade_id: str, *, status: str, exit_price: float | None = None) -> dict[str, Any]:
    return {
        "trade_id": trade_id,
        "basket_id": "basket-1",
        "status": status,
        "symbol": "BTCUSDT",
        "side": "short",
        "date": "2026-01-15",
        "entry_ts_ms": int(NOW.timestamp() * 1000),
        "entry_price": 100.0,
        "mark_price": 99.5,
        "exit_price": exit_price,
        "exit_reason": "max_hold" if status == "closed" else "open",
        "actual_notional": 2_000.0,
        "target_notional": 2_000.0,
    }


class _FakeMarket:
    def get_instruments_info(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "BTCUSDT",
                "status": "Trading",
                "priceFilter": {"tickSize": "0.1"},
                "lotSizeFilter": {
                    "qtyStep": "0.001",
                    "minOrderQty": "0.001",
                    "minNotionalValue": "5",
                },
            }
        ]

    def get_orderbook(self, symbol: str, limit: int = 1) -> dict[str, list[list[str]]]:
        del symbol, limit
        return {"b": [["100", "1"]], "a": [["101", "1"]]}


class _FakeExecution:
    def __init__(self, *, position_size: float = 0.0, position_value: float = 0.0) -> None:
        self.placed: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self.position_size = position_size
        self.position_value = position_value

    def place_order(self, **params: Any) -> dict[str, Any]:
        self.placed.append(params)
        return {"orderLinkId": params["orderLinkId"], "orderId": f"demo-{len(self.placed)}"}

    def cancel_order(self, *, symbol: str, order_link_id: str) -> dict[str, str]:
        self.cancelled.append({"symbol": symbol, "order_link_id": order_link_id})
        return {"orderLinkId": order_link_id}

    def get_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        del symbol
        return []

    def get_positions(self, *, symbol: str | None = None) -> list[dict[str, str]]:
        if self.position_size <= 0.0:
            return []
        return [
            {
                "symbol": symbol or "BTCUSDT",
                "side": "Sell",
                "size": str(self.position_size),
                "positionValue": str(self.position_value),
            }
        ]
