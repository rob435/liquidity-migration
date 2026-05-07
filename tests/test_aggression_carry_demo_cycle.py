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
        cycle_config=DemoCycleConfig(),
        now=NOW,
        forward_runner=_forward_runner_open_trade,
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert payload["rows"]["sleeves"] == 1
    assert payload["summary"]["dry_run"] == 1
    assert payload["summary"]["skipped"] == 0
    assert payload["summary"]["placed"] == 0
    assert payload["config"]["entry_sleeves"] == ("rank_31_plus",)
    assert execution.placed == []
    for sleeve in DEMO_CYCLE_SLEEVES:
        sleeve_root = tmp_path / "forward_sleeves" / sleeve
        orders = read_dataset(sleeve_root, "demo_execution_orders")
        assert orders.height == 1
        row = orders.row(0, named=True)
        if sleeve == "rank_31_plus":
            assert row["status"] == "dry_run"
        else:
            assert row["status"] == "skipped"
            assert row["error"] == "new_entries_paused"
        assert (sleeve_root / "reports" / "bybit_demo_sync_report.md").exists()
    assert (tmp_path / "reports" / "bybit_demo_cycle_report.json").exists()
    assert (tmp_path / "reports" / "bybit_demo_cycle_report.md").exists()


def test_demo_cycle_uses_paper_notional_without_demo_cap(tmp_path: Path) -> None:
    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(),
        now=NOW,
        forward_runner=_forward_runner_open_trade,
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )

    sleeve_root = tmp_path / "forward_sleeves" / "rank_31_plus"
    row = read_dataset(sleeve_root, "demo_execution_orders").row(0, named=True)

    assert payload["summary"]["dry_run"] == 1
    assert row["estimated_notional"] > 10.0
    assert row["max_order_notional"] == 2_000.0


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


def test_demo_cycle_default_submit_entries_are_rank_31_plus_only(tmp_path: Path) -> None:
    execution = _FakeExecution()

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(submit_orders=True, confirmed=True),
        now=NOW,
        forward_runner=_forward_runner_open_trade,
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    order_link_ids = [order["orderLinkId"] for order in execution.placed]
    assert payload["summary"]["placed"] == 1
    assert payload["summary"]["skipped"] == 0
    assert len(order_link_ids) == 1
    assert order_link_ids[0].startswith("agcr31pe")
    assert execution.leverage_calls == [{"symbol": "BTCUSDT", "buy_leverage": 1.0, "sell_leverage": 1.0}]


def test_demo_cycle_rejects_unknown_entry_sleeve(tmp_path: Path) -> None:
    try:
        run_bybit_demo_cycle(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            cycle_config=DemoCycleConfig(entry_sleeves=("not_a_sleeve",)),
            now=NOW,
            forward_runner=_forward_runner_open_trade,
            market_client=_FakeMarket(),
            execution_client=_FakeExecution(),
        )
    except ValueError as exc:
        assert "unknown demo entry sleeve" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("unknown demo entry sleeve should be rejected")


def test_demo_cycle_pause_blocks_entries_but_allows_reduce_only_exits(tmp_path: Path) -> None:
    (tmp_path / "DEMO_PAUSED").write_text("maintenance", encoding="utf-8")
    _forward_runner_open_and_closed_trades(tmp_path)
    execution = _FakeExecution(position_size=0.05, position_value=5.0)

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(submit_orders=True, confirmed=True),
        now=NOW,
        forward_runner=_forward_runner_open_and_closed_trades,
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert payload["paused"]["paused"] is True
    assert payload["summary"]["new_orders"] == 2
    assert payload["summary"]["accepted"] == 1
    assert payload["summary"]["skipped"] == 1
    assert payload["summary"]["dry_run"] == 0
    assert len(execution.placed) == 1
    assert {order["orderType"] for order in execution.placed} == {"Limit"}
    assert {order["timeInForce"] for order in execution.placed} == {"IOC"}
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


def test_demo_cycle_replaces_lock_when_recorded_pid_is_dead(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".bybit_demo_cycle.lock").write_text('{"pid": 123456, "started": "2026-01-15T22:00:00+00:00"}\n', encoding="utf-8")
    monkeypatch.setattr("aggression_carry.demo_cycle._pid_is_running", lambda pid: False)

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(),
        now=NOW,
        forward_runner=_forward_runner_open_trade,
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )

    assert payload["rows"]["sleeves"] == 1
    assert not (tmp_path / ".bybit_demo_cycle.lock").exists()


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


def test_demo_cycle_outside_window_with_demo_active_state_skips_scan_but_reconciles(tmp_path: Path) -> None:
    _write_existing_demo_entry(tmp_path / "forward_sleeves" / "rank_31_plus", "paper-entry-rank31")
    forward_called = False
    sync_calls = 0

    def forward_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal forward_called
        forward_called = True
        return _forward_runner_open_trade(*args, **kwargs)

    def sync_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal sync_calls
        sync_calls += 1
        return {"rows": {"new_orders": 0, "ledger_orders": 1}, "summary": {"placed": 1, "accepted": 1}}

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
    assert sync_calls == 1
    assert payload["active_window"]["existing_active_state"]["demo_active"] == 1
    assert payload["summary"]["ledger_orders"] == 1


def test_demo_cycle_paused_active_window_with_demo_active_state_skips_scan_but_reconciles(tmp_path: Path) -> None:
    (tmp_path / "DEMO_PAUSED").write_text("maintenance", encoding="utf-8")
    _write_existing_demo_entry(tmp_path / "forward_sleeves" / "rank_31_plus", "paper-entry-rank31")
    forward_called = False
    sync_calls = 0

    def forward_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal forward_called
        forward_called = True
        return _forward_runner_open_trade(*args, **kwargs)

    def sync_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal sync_calls
        sync_calls += 1
        return {"rows": {"new_orders": 0, "ledger_orders": 1}, "summary": {"placed": 1, "accepted": 1}}

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(),
        now=NOW,
        forward_runner=forward_runner,
        sync_runner=sync_runner,
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )

    assert forward_called is False
    assert sync_calls == 1
    assert payload["active_window"]["inside"] is True
    assert payload["active_window"]["entries_paused"] is True
    assert payload["active_window"]["existing_active_state"]["demo_active"] == 1
    assert payload["summary"]["ledger_orders"] == 1


def test_demo_cycle_runs_fast_protection_before_forward_mark_and_sync(tmp_path: Path, monkeypatch) -> None:
    sleeve_root = tmp_path / "forward_sleeves" / "rank_31_plus"
    _write_forward_trades(
        sleeve_root,
        [
            {
                **_paper_trade("paper-active", status="open"),
                "profit_protection_active_ts_ms": int(NOW.timestamp() * 1000) - 60_000,
            }
        ],
    )
    _write_existing_demo_entry(sleeve_root, "paper-active")
    calls: list[str] = []

    def fake_fast(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        calls.append("fast")
        return {"rows": {"active_trades": 1}, "summary": {}}

    def forward_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("forward")
        return _forward_runner_open_trade(*args, **kwargs)

    def sync_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        calls.append("sync")
        return {"rows": {"new_orders": 0, "ledger_orders": 1}, "summary": {"placed": 0, "accepted": 0}}

    monkeypatch.setattr("aggression_carry.demo_cycle.run_demo_fast_protection", fake_fast)

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(submit_orders=True, confirmed=True, fast_protection_seconds=55),
        now=NOW,
        forward_runner=forward_runner,
        sync_runner=sync_runner,
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(position_size=0.05, position_value=5.0),
    )

    assert calls == ["fast", "forward", "sync"]
    assert payload["sleeves"][0]["fast_protection"]["rows"]["active_trades"] == 1


def test_demo_cycle_marks_fast_protection_failure_as_failed_sleeve(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_fast(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        calls.append("fast")
        raise RuntimeError("stream down")

    def sync_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        calls.append("sync")
        return {"rows": {"new_orders": 0, "ledger_orders": 1}, "summary": {"placed": 0, "accepted": 0}}

    monkeypatch.setattr("aggression_carry.demo_cycle.run_demo_fast_protection", fake_fast)

    payload = run_bybit_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cycle_config=DemoCycleConfig(submit_orders=True, confirmed=True, fast_protection_seconds=55),
        now=NOW,
        forward_runner=_forward_runner_open_trade,
        sync_runner=sync_runner,
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(position_size=0.05, position_value=5.0),
    )

    assert calls == ["fast", "sync"]
    assert payload["rows"]["failed_sleeves"] == 1
    assert payload["sleeves"][0]["status"] == "failed"
    assert payload["sleeves"][0]["error"] == "stream down"


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
        self.leverage_calls: list[dict[str, Any]] = []
        self.position_size = position_size
        self.position_value = position_value

    def set_leverage(self, *, symbol: str, buy_leverage: float, sell_leverage: float) -> dict[str, Any]:
        self.leverage_calls.append({"symbol": symbol, "buy_leverage": buy_leverage, "sell_leverage": sell_leverage})
        return {"symbol": symbol, "buyLeverage": str(buy_leverage), "sellLeverage": str(sell_leverage)}

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
