from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from aggression_carry.config import ResearchConfig
from aggression_carry.demo_execution import DemoProbeConfig, DemoSyncConfig, run_bybit_demo_probe, run_bybit_demo_sync
from aggression_carry.storage import read_dataset, write_dataset


def test_demo_probe_dry_run_builds_far_post_only_order(tmp_path: Path) -> None:
    payload = run_bybit_demo_probe(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        probe_config=DemoProbeConfig(symbol="BTCUSDT", side="Sell", notional=5.0, place_order=False),
        now=datetime(2026, 1, 15, tzinfo=UTC),
        market_client=_FakeMarket(),
    )

    request = payload["order"]["request"]
    assert payload["status"] == "dry_run"
    assert request["symbol"] == "BTCUSDT"
    assert request["side"] == "Sell"
    assert request["timeInForce"] == "PostOnly"
    assert float(request["price"]) > 101.0
    assert float(request["qty"]) > 0.0
    assert (tmp_path / "reports" / "bybit_demo_probe_report.md").exists()


def test_demo_probe_place_order_requires_confirmation(tmp_path: Path) -> None:
    try:
        run_bybit_demo_probe(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            probe_config=DemoProbeConfig(symbol="BTCUSDT", place_order=True, confirmed=False),
            now=datetime(2026, 1, 15, tzinfo=UTC),
            market_client=_FakeMarket(),
            execution_client=_FakeExecution(),
        )
    except RuntimeError as exc:
        assert "--i-understand-demo-order" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("demo order placement should require confirmation")


def test_demo_probe_rejects_minimum_order_above_cap(tmp_path: Path) -> None:
    try:
        run_bybit_demo_probe(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            probe_config=DemoProbeConfig(symbol="BTCUSDT", notional=10.0, max_notional=10.0),
            now=datetime(2026, 1, 15, tzinfo=UTC),
            market_client=_FakeExpensiveMarket(),
        )
    except ValueError as exc:
        assert "above max_notional" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("minimum order above max cap should be rejected")


def test_demo_probe_places_and_cancels_with_fake_execution(tmp_path: Path) -> None:
    execution = _FakeExecution()
    payload = run_bybit_demo_probe(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        probe_config=DemoProbeConfig(
            symbol="BTCUSDT",
            side="Buy",
            notional=5.0,
            max_notional=10.0,
            place_order=True,
            cancel_order=True,
            confirmed=True,
        ),
        now=datetime(2026, 1, 15, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert payload["status"] == "placed_cancel_requested"
    assert execution.placed[0]["side"] == "Buy"
    assert execution.cancelled[0]["order_link_id"] == execution.placed[0]["orderLinkId"]


def test_demo_sync_dry_run_writes_capped_entry_without_private_calls(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    execution = _FakeExecution()

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=False, max_order_notional=11.0),
        now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    orders = read_dataset(tmp_path, "demo_execution_orders")

    assert payload["rows"]["new_orders"] == 1
    assert orders.row(0, named=True)["status"] == "dry_run"
    assert orders.row(0, named=True)["action"] == "entry"
    assert orders.row(0, named=True)["side"] == "Sell"
    assert orders.row(0, named=True)["estimated_notional"] <= 11.0
    assert execution.placed == []
    assert (tmp_path / "reports" / "bybit_demo_sync_report.md").exists()


def test_demo_sync_requires_confirmation_before_submit(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    try:
        run_bybit_demo_sync(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            sync_config=DemoSyncConfig(submit_orders=True, confirmed=False),
            now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
            market_client=_FakeMarket(),
            execution_client=_FakeExecution(),
        )
    except RuntimeError as exc:
        assert "--i-understand-demo-sync" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("demo sync order placement should require confirmation")


def test_demo_sync_places_entry_once(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    execution = _FakeExecution()
    config = ResearchConfig(data_root=tmp_path)
    sync_config = DemoSyncConfig(submit_orders=True, confirmed=True, max_order_notional=11.0)

    first = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=sync_config,
        now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    second = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=sync_config,
        now=datetime(2026, 1, 15, 22, 17, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert first["rows"]["new_orders"] == 1
    assert second["rows"]["new_orders"] == 0
    assert len(execution.placed) == 1
    assert execution.placed[0]["timeInForce"] == "PostOnly"


def test_demo_sync_closed_paper_trade_places_reduce_only_exit(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="closed", exit_price=99.0)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agcexisting",
                    "paper_trade_id": "paper-1",
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
                    "max_order_notional": 6.0,
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 16, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:16:00+00:00",
                }
            ]
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )
    execution = _FakeExecution(position_size=0.05, position_value=5.0)

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True, max_order_notional=6.0),
        now=datetime(2026, 1, 15, 22, 40, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert payload["rows"]["new_orders"] == 1
    assert execution.placed[-1]["side"] == "Buy"
    assert execution.placed[-1]["orderType"] == "Market"
    assert execution.placed[-1]["reduceOnly"] is True


def test_demo_sync_cancels_stale_open_entry_order(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-stale",
                    "paper_trade_id": "paper-1",
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
                    "max_order_notional": 10.0,
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 16, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:16:00+00:00",
                }
            ]
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )
    execution = _FakeExecution(open_order_link_id="agc-stale")

    run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True, cancel_stale_minutes=0),
        now=datetime(2026, 1, 15, 22, 17, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert execution.cancelled == [{"symbol": "BTCUSDT", "order_link_id": "agc-stale"}]


def _write_paper_trade(tmp_path: Path, *, status: str, exit_price: float | None = None) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "status": status,
                    "symbol": "BTCUSDT",
                    "side": "short",
                    "date": "2026-01-15",
                    "entry_ts_ms": int(datetime(2026, 1, 15, 22, 16, tzinfo=UTC).timestamp() * 1000),
                    "entry_price": 100.0,
                    "mark_price": 99.5,
                    "exit_price": exit_price,
                    "exit_reason": "max_hold" if status == "closed" else "open",
                    "actual_notional": 2_000.0,
                    "target_notional": 2_000.0,
                }
            ]
        ),
        tmp_path,
        "forward_paper_trades",
        partition_by=("date", "symbol"),
        append=False,
    )


class _FakeMarket:
    def get_instruments_info(self) -> list[dict]:
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

    def get_orderbook(self, symbol: str, limit: int = 1) -> dict:
        del symbol, limit
        return {"b": [["100", "1"]], "a": [["101", "1"]]}


class _FakeExpensiveMarket(_FakeMarket):
    def get_orderbook(self, symbol: str, limit: int = 1) -> dict:
        del symbol, limit
        return {"b": [["78000", "1"]], "a": [["78001", "1"]]}


class _FakeExecution:
    def __init__(
        self,
        *,
        position_size: float = 0.0,
        position_value: float = 0.0,
        open_order_link_id: str = "",
    ) -> None:
        self.placed: list[dict] = []
        self.cancelled: list[dict] = []
        self.position_size = position_size
        self.position_value = position_value
        self.open_order_link_id = open_order_link_id

    def get_wallet_balance(self, *, account_type: str, coin: str) -> dict:
        return {"accountType": account_type, "coin": coin}

    def place_order(self, **params):
        self.placed.append(params)
        return {"orderLinkId": params["orderLinkId"], "orderId": "demo-order"}

    def cancel_order(self, *, symbol: str, order_link_id: str):
        self.cancelled.append({"symbol": symbol, "order_link_id": order_link_id})
        return {"orderLinkId": order_link_id}

    def get_open_orders(self, *, symbol: str | None = None) -> list[dict]:
        if not self.open_order_link_id:
            return []
        return [{"symbol": symbol or "BTCUSDT", "orderLinkId": self.open_order_link_id, "orderStatus": "New"}]

    def get_positions(self, *, symbol: str | None = None) -> list[dict]:
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
