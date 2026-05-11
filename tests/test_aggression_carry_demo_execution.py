from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from aggression_carry.config import ResearchConfig
from aggression_carry.demo_execution import (
    DemoCancelAllConfig,
    DemoFlattenConfig,
    DemoProbeConfig,
    DemoSyncConfig,
    run_bybit_demo_cancel_all,
    run_bybit_demo_flatten,
    run_bybit_demo_probe,
    run_bybit_demo_sync,
)
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


def test_demo_sync_dry_run_writes_paper_sized_entry_without_private_calls(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    execution = _FakeExecution()

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=False),
        now=datetime(2026, 1, 15, 22, 16, 37, 250000, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    orders = read_dataset(tmp_path, "demo_execution_orders")

    assert payload["rows"]["new_orders"] == 1
    assert orders.row(0, named=True)["status"] == "dry_run"
    assert orders.row(0, named=True)["action"] == "entry"
    assert orders.row(0, named=True)["side"] == "Sell"
    assert orders.row(0, named=True)["estimated_notional"] > 1_900.0
    assert orders.row(0, named=True)["max_order_notional"] == 2_000.0
    assert execution.placed == []
    assert (tmp_path / "reports" / "bybit_demo_sync_report.md").exists()


def test_demo_sync_twap_entry_order_matches_due_paper_slice(tmp_path: Path) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status="open", target_notional=6_000.0, entry_twap_minutes=60)])
    _write_paper_slices(
        tmp_path,
        [
            _paper_slice(1, datetime(2026, 1, 15, 22, 15, tzinfo=UTC)),
            _paper_slice(2, datetime(2026, 1, 15, 22, 16, tzinfo=UTC)),
            _paper_slice(3, datetime(2026, 1, 15, 22, 17, tzinfo=UTC), status="pending"),
        ],
    )

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(
            submit_orders=False,
            order_link_prefix="r31p",
            require_contiguous_twap=False,
        ),
        now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )
    orders = read_dataset(tmp_path, "demo_execution_orders")
    row = orders.row(0, named=True)

    assert payload["rows"]["paper_slices"] == 3
    assert payload["rows"]["new_orders"] == 1
    assert row["slice_index"] == 2
    assert row["slice_time"] == "2026-01-15T22:16:00+00:00"
    assert row["created_time"] == "2026-01-15T22:16:00+00:00"
    assert row["order_link_id"].startswith("agcr31pe")
    assert "btcusdt" in row["order_link_id"]
    assert "260115" in row["order_link_id"]
    assert "2216" in row["order_link_id"]
    assert 99.0 <= row["estimated_notional"] <= 101.0


def test_demo_sync_backtest_sizing_uses_paper_child_notional_without_demo_cap(tmp_path: Path) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status="open", target_notional=6_000.0, entry_twap_minutes=60)])
    _write_paper_slices(tmp_path, [_paper_slice(1, datetime(2026, 1, 15, 22, 1, tzinfo=UTC))])

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(
            submit_orders=False,
            order_link_prefix="r31p",
        ),
        now=datetime(2026, 1, 15, 22, 1, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )
    row = read_dataset(tmp_path, "demo_execution_orders").row(0, named=True)

    assert payload["rows"]["new_orders"] == 1
    assert row["status"] == "dry_run"
    assert row["estimated_notional"] > 10.0
    assert 99.0 <= row["estimated_notional"] <= 101.0
    assert 99.0 <= row["max_order_notional"] <= 101.0


def test_demo_sync_refuses_late_twap_start_without_prior_slice_attempt(tmp_path: Path) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status="open", target_notional=6_000.0, entry_twap_minutes=60)])
    _write_paper_slices(
        tmp_path,
        [_paper_slice(index, datetime(2026, 1, 15, 22, index, tzinfo=UTC)) for index in range(1, 7)],
    )

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 6, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )
    orders = read_dataset(tmp_path, "demo_execution_orders")
    row = orders.row(0, named=True)

    assert payload["rows"]["new_orders"] == 1
    assert row["slice_index"] == 6
    assert row["status"] == "skipped"
    assert row["error"] == "missed_prior_twap_slice"


def test_demo_sync_allows_contiguous_twap_slice_after_prior_attempt(tmp_path: Path) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status="open", target_notional=6_000.0, entry_twap_minutes=60)])
    _write_paper_slices(
        tmp_path,
        [
            _paper_slice(1, datetime(2026, 1, 15, 22, 1, tzinfo=UTC)),
            _paper_slice(2, datetime(2026, 1, 15, 22, 2, tzinfo=UTC)),
        ],
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-existing-slice-1",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "slice_index": 1,
                    "slice_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "status": "accepted",
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "order_type": "Limit",
                    "time_in_force": "PostOnly",
                    "qty": "1",
                    "price": "101.1",
                    "reduce_only": False,
                    "estimated_notional": 101.1,
                    "max_order_notional": 200.0,
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:01:00+00:00",
                }
            ]
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )

    execution = _FakeExecution()
    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 2, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    orders = read_dataset(tmp_path, "demo_execution_orders").sort("slice_index")

    assert payload["rows"]["new_orders"] == 1
    assert orders.row(1, named=True)["slice_index"] == 2
    assert orders.row(1, named=True)["status"] == "accepted"
    assert execution.leverage_calls == []


def test_demo_sync_place_failed_prior_slice_allows_next_scheduled_slice(tmp_path: Path) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status="open", target_notional=6_000.0, entry_twap_minutes=60)])
    _write_paper_slices(
        tmp_path,
        [
            _paper_slice(1, datetime(2026, 1, 15, 22, 1, tzinfo=UTC)),
            _paper_slice(2, datetime(2026, 1, 15, 22, 2, tzinfo=UTC)),
        ],
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-failed-slice-1",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "slice_index": 1,
                    "slice_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "status": "place_failed",
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "order_type": "Limit",
                    "time_in_force": "PostOnly",
                    "qty": "1",
                    "price": "101.1",
                    "reduce_only": False,
                    "estimated_notional": 101.1,
                    "max_order_notional": 200.0,
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:01:00+00:00",
                    "request": json.dumps(
                        {
                            "symbol": "BTCUSDT",
                            "side": "Sell",
                            "orderType": "Limit",
                            "qty": "1",
                            "price": "101.1",
                            "timeInForce": "PostOnly",
                            "orderLinkId": "agc-failed-slice-1",
                        }
                    ),
                    "error": "simulated submit prep failure",
                }
            ]
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 2, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )
    orders = read_dataset(tmp_path, "demo_execution_orders").sort("slice_index")

    assert payload["rows"]["new_orders"] == 1
    assert orders.row(1, named=True)["slice_index"] == 2
    assert orders.row(1, named=True)["status"] == "accepted"


def test_demo_sync_recorded_missed_slice_allows_later_scheduled_slice(tmp_path: Path) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status="open", target_notional=6_000.0, entry_twap_minutes=60)])
    _write_paper_slices(
        tmp_path,
        [
            _paper_slice(1, datetime(2026, 1, 15, 22, 1, tzinfo=UTC)),
            _paper_slice(2, datetime(2026, 1, 15, 22, 2, tzinfo=UTC)),
            _paper_slice(3, datetime(2026, 1, 15, 22, 3, tzinfo=UTC)),
        ],
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-existing-slice-1",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "slice_index": 1,
                    "slice_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "status": "accepted",
                    "symbol": "BTCUSDT",
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:01:00+00:00",
                },
                {
                    "order_link_id": "agc-missed-slice-2",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "slice_index": 2,
                    "slice_ts_ms": int(datetime(2026, 1, 15, 22, 2, tzinfo=UTC).timestamp() * 1000),
                    "status": "skipped",
                    "symbol": "BTCUSDT",
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 2, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:02:00+00:00",
                    "error": "missed_prior_twap_slice",
                },
            ]
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 3, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )
    orders = read_dataset(tmp_path, "demo_execution_orders").sort("slice_index")

    assert payload["rows"]["new_orders"] == 1
    assert orders.row(2, named=True)["slice_index"] == 3
    assert orders.row(2, named=True)["status"] == "accepted"


def test_demo_sync_terminal_missed_prior_slice_allows_next_scheduled_slice(tmp_path: Path) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status="open", target_notional=6_000.0, entry_twap_minutes=60)])
    _write_paper_slices(
        tmp_path,
        [
            _paper_slice(1, datetime(2026, 1, 15, 22, 1, tzinfo=UTC)),
            _paper_slice(2, datetime(2026, 1, 15, 22, 2, tzinfo=UTC)),
        ],
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-missed-slice-1",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "slice_index": 1,
                    "slice_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "status": "cancel_requested",
                    "reconciled_status": "missed_entry",
                    "symbol": "BTCUSDT",
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:01:00+00:00",
                }
            ]
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 2, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )
    orders = read_dataset(tmp_path, "demo_execution_orders").sort("slice_index")

    assert payload["rows"]["new_orders"] == 1
    assert orders.row(1, named=True)["slice_index"] == 2
    assert orders.row(1, named=True)["status"] == "accepted"


def test_demo_sync_dry_run_prior_slice_allows_next_dry_run_slice(tmp_path: Path) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status="open", target_notional=6_000.0, entry_twap_minutes=60)])
    _write_paper_slices(
        tmp_path,
        [
            _paper_slice(1, datetime(2026, 1, 15, 22, 1, tzinfo=UTC)),
            _paper_slice(2, datetime(2026, 1, 15, 22, 2, tzinfo=UTC)),
        ],
    )
    config = ResearchConfig(data_root=tmp_path)
    sync_config = DemoSyncConfig(submit_orders=False)

    first = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=sync_config,
        now=datetime(2026, 1, 15, 22, 1, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )
    second = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=sync_config,
        now=datetime(2026, 1, 15, 22, 2, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )
    orders = read_dataset(tmp_path, "demo_execution_orders").sort("slice_index")

    assert first["rows"]["new_orders"] == 1
    assert second["rows"]["new_orders"] == 1
    assert orders.height == 2
    assert orders.select("slice_index").to_series().to_list() == [1, 2]
    assert orders.select("status").to_series().to_list() == ["dry_run", "dry_run"]


def test_demo_sync_dry_run_prior_slice_does_not_allow_private_submit(tmp_path: Path) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status="open", target_notional=6_000.0, entry_twap_minutes=60)])
    _write_paper_slices(
        tmp_path,
        [
            _paper_slice(1, datetime(2026, 1, 15, 22, 1, tzinfo=UTC)),
            _paper_slice(2, datetime(2026, 1, 15, 22, 2, tzinfo=UTC)),
        ],
    )
    config = ResearchConfig(data_root=tmp_path)
    run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=DemoSyncConfig(submit_orders=False),
        now=datetime(2026, 1, 15, 22, 1, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=_FakeExecution(),
    )
    execution = _FakeExecution()

    submitted = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=DemoSyncConfig(
            submit_orders=True,
            confirmed=True,
        ),
        now=datetime(2026, 1, 15, 22, 2, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    orders = read_dataset(tmp_path, "demo_execution_orders").sort("created_ts_ms")

    assert submitted["rows"]["new_orders"] == 1
    assert orders.row(-1, named=True)["slice_index"] == 2
    assert orders.row(-1, named=True)["status"] == "skipped"
    assert orders.row(-1, named=True)["error"] == "missed_prior_twap_slice"
    assert execution.place_attempts == []


def test_demo_sync_dry_run_does_not_block_later_submit(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    config = ResearchConfig(data_root=tmp_path)
    dry_run_execution = _FakeExecution()

    dry_run = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=DemoSyncConfig(submit_orders=False),
        now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=dry_run_execution,
    )
    submit_execution = _FakeExecution()
    submitted = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 17, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=submit_execution,
    )
    orders = read_dataset(tmp_path, "demo_execution_orders")

    assert dry_run["rows"]["new_orders"] == 1
    assert submitted["rows"]["new_orders"] == 1
    assert dry_run_execution.place_attempts == []
    assert len(submit_execution.placed) == 1
    assert submit_execution.leverage_calls == [{"symbol": "BTCUSDT", "buy_leverage": 1.0, "sell_leverage": 1.0}]
    assert orders.row(0, named=True)["status"] == "accepted"


def test_demo_sync_entry_leverage_failure_blocks_private_submit(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    execution = _FakeExecution(fail_set_leverage=True)

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert payload["new_orders"][0]["status"] == "place_failed"
    assert "simulated leverage failure" in payload["new_orders"][0]["error"]
    assert execution.place_attempts == []
    assert execution.placed == []


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
    sync_config = DemoSyncConfig(submit_orders=True, confirmed=True)

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


def test_demo_sync_skipped_entry_does_not_block_retry(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    config = ResearchConfig(data_root=tmp_path)

    skipped = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=DemoSyncConfig(submit_orders=False),
        now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
        market_client=_FakeMissingMarket(),
    )
    execution = _FakeExecution()
    submitted = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 17, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert skipped["new_orders"][0]["status"] == "skipped"
    assert skipped["new_orders"][0]["error"] == "instrument_missing"
    assert submitted["rows"]["new_orders"] == 1
    assert len(execution.placed) == 1


def test_demo_sync_ambiguous_place_failure_blocks_until_reconciled_not_found(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    execution = _FakeExecution(fail_place_times=1)
    config = ResearchConfig(data_root=tmp_path)
    sync_config = DemoSyncConfig(submit_orders=True, confirmed=True)

    unknown = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=sync_config,
        now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    blocked = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=sync_config,
        now=datetime(2026, 1, 15, 22, 16, 30, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    retried_after_grace = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=sync_config,
        now=datetime(2026, 1, 15, 22, 18, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert unknown["new_orders"][0]["status"] == "submit_unknown"
    assert blocked["rows"]["new_orders"] == 0
    assert retried_after_grace["new_orders"][0]["status"] == "accepted"
    assert len(execution.place_attempts) == 2
    assert len(execution.placed) == 1


def test_demo_sync_ambiguous_place_failure_reconciles_open_order_without_retry(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    execution = _FakeExecution(fail_place_times=1)
    config = ResearchConfig(data_root=tmp_path)
    sync_config = DemoSyncConfig(submit_orders=True, confirmed=True)

    unknown = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=sync_config,
        now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    execution.open_order_link_id = unknown["new_orders"][0]["order_link_id"]
    reconciled = run_bybit_demo_sync(
        tmp_path,
        config=config,
        sync_config=sync_config,
        now=datetime(2026, 1, 15, 22, 17, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    orders = read_dataset(tmp_path, "demo_execution_orders")

    assert reconciled["rows"]["new_orders"] == 0
    assert orders.row(0, named=True)["reconciled_status"] == "open_order_seen"


def test_demo_sync_keeps_polling_filled_order_until_execution_details_arrive(tmp_path: Path) -> None:
    _write_paper_trade(tmp_path, status="open")
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-filled-lag",
                    "order_id": "demo-entry",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "status": "accepted",
                    "reconciled_status": "accepted",
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
    execution = _FilledOrderWithoutTradesExecution()

    run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 17, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 18, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )
    orders = read_dataset(tmp_path, "demo_execution_orders")

    assert orders.row(0, named=True)["reconciled_status"] == "filled_pending_execs"
    assert execution.order_history_calls >= 2
    assert execution.trade_history_calls >= 2
    assert len(execution.place_attempts) == 0
    assert execution.placed == []


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
                    "created_time": "2026-01-15T22:06:00+00:00",
                }
            ]
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )
    execution = _FakeExecution(position_size=0.08, position_value=8.0, open_order_link_id="agcexisting", position_idx=2)

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(
            submit_orders=True,
            confirmed=True,
        ),
        now=datetime(2026, 1, 15, 22, 40, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert payload["rows"]["new_orders"] == 1
    assert execution.cancelled == [{"symbol": "BTCUSDT", "order_link_id": "agcexisting"}]
    assert execution.placed[-1]["side"] == "Buy"
    assert execution.placed[-1]["orderType"] == "Limit"
    assert execution.placed[-1]["timeInForce"] == "IOC"
    assert execution.placed[-1]["price"] == "101.6"
    assert execution.placed[-1]["qty"] == "0.08"
    assert execution.placed[-1]["positionIdx"] == 2
    assert execution.placed[-1]["reduceOnly"] is True


def test_demo_sync_retries_remaining_exit_after_partial_ioc_cancel(tmp_path: Path) -> None:
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
                    "status": "accepted",
                    "reconciled_status": "filled",
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "order_type": "Limit",
                    "time_in_force": "PostOnly",
                    "qty": "0.08",
                    "price": "101.1",
                    "reduce_only": False,
                    "estimated_notional": 8.088,
                    "max_order_notional": 8.088,
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 16, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:16:00+00:00",
                },
                {
                    "order_link_id": "agcpriorpartial",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "exit",
                    "status": "accepted",
                    "reconciled_status": "partial_cancelled",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "order_type": "Limit",
                    "time_in_force": "IOC",
                    "qty": "0.08",
                    "price": "99.5",
                    "reduce_only": True,
                    "estimated_notional": 8.0,
                    "max_order_notional": 8.0,
                    "filled_qty": 0.03,
                    "filled_value": 2.985,
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 39, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:39:00+00:00",
                },
            ],
            infer_schema_length=None,
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )
    execution = _FakeExecution(position_size=0.05, position_value=5.0)

    run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 15, 22, 40, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert len(execution.placed) == 1
    assert execution.placed[0]["orderLinkId"] != "agcpriorpartial"
    assert execution.placed[0]["qty"] == "0.05"


def test_demo_sync_prioritizes_reduce_only_exits_before_entries(tmp_path: Path) -> None:
    _write_paper_trades(
        tmp_path,
        [
            _paper_trade(status="closed", trade_id="paper-1", symbol="BTCUSDT", exit_price=99.0),
            _paper_trade(status="open", trade_id="paper-2", symbol="ETHUSDT"),
        ],
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-existing-entry",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "status": "accepted",
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
                    "created_time": "2026-01-15T22:06:00+00:00",
                }
            ]
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )
    execution = _FakeExecution(position_size=0.05, position_value=5.0)

    run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True, max_new_orders=1),
        now=datetime(2026, 1, 15, 22, 40, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert len(execution.placed) == 1
    assert execution.placed[0]["symbol"] == "BTCUSDT"
    assert execution.placed[0]["reduceOnly"] is True


def test_demo_sync_pause_new_entries_still_allows_exit(tmp_path: Path) -> None:
    _write_paper_trades(
        tmp_path,
        [
            _paper_trade(status="closed", trade_id="paper-1", symbol="BTCUSDT", exit_price=99.0),
            _paper_trade(status="open", trade_id="paper-2", symbol="ETHUSDT"),
        ],
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-existing-entry",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "status": "accepted",
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
                    "created_time": "2026-01-15T22:06:00+00:00",
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
        sync_config=DemoSyncConfig(
            submit_orders=True,
            confirmed=True,
            pause_new_entries=True,
        ),
        now=datetime(2026, 1, 15, 22, 40, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert len(execution.placed) == 1
    assert execution.placed[0]["reduceOnly"] is True
    assert {row["error"] for row in payload["new_orders"] if row["status"] == "skipped"} == {"new_entries_paused"}


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
                    "created_time": "2026-01-15T22:06:00+00:00",
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


def test_demo_sync_does_not_reconcile_terminal_old_orders(tmp_path: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-filled-old",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "slice_index": 1,
                    "slice_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "status": "accepted",
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "order_type": "Limit",
                    "time_in_force": "PostOnly",
                    "qty": "1",
                    "price": "101.1",
                    "reduce_only": False,
                    "estimated_notional": 101.1,
                    "max_order_notional": 200.0,
                    "created_ts_ms": int(datetime(2026, 1, 15, 22, 1, tzinfo=UTC).timestamp() * 1000),
                    "created_time": "2026-01-15T22:01:00+00:00",
                    "reconciled_status": "filled",
                    "filled_qty": 1.0,
                    "filled_value": 101.1,
                }
            ]
        ),
        tmp_path,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )
    execution = _CountingExecution()

    payload = run_bybit_demo_sync(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        sync_config=DemoSyncConfig(submit_orders=True, confirmed=True),
        now=datetime(2026, 1, 16, 0, 0, tzinfo=UTC),
        market_client=_FakeMarket(),
        execution_client=execution,
    )

    assert payload["rows"]["existing_orders"] == 1
    assert payload["rows"]["new_orders"] == 0
    assert execution.private_reads == 0


def test_demo_cancel_all_uses_demo_client_call(tmp_path: Path) -> None:
    execution = _FakeExecution()

    payload = run_bybit_demo_cancel_all(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        cancel_config=DemoCancelAllConfig(symbols=("BTCUSDT", "ETHUSDT"), confirmed=True),
        now=datetime(2026, 1, 15, 22, 40, tzinfo=UTC),
        execution_client=execution,
    )

    assert payload["rows"]["cancel_requested"] == 2
    assert execution.cancel_all == [
        {"symbol": "BTCUSDT", "settle_coin": "USDT"},
        {"symbol": "ETHUSDT", "settle_coin": "USDT"},
    ]
    assert (tmp_path / "reports" / "bybit_demo_cancel_all_report.md").exists()


def test_demo_cancel_all_requires_confirmation(tmp_path: Path) -> None:
    try:
        run_bybit_demo_cancel_all(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            cancel_config=DemoCancelAllConfig(symbols=("BTCUSDT",), confirmed=False),
            now=datetime(2026, 1, 15, 22, 40, tzinfo=UTC),
            execution_client=_FakeExecution(),
        )
    except RuntimeError as exc:
        assert "--i-understand-demo-cancel-all" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("demo cancel-all should require confirmation")


def test_demo_flatten_requires_confirmation(tmp_path: Path) -> None:
    try:
        run_bybit_demo_flatten(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            flatten_config=DemoFlattenConfig(confirmed=False),
            now=datetime(2026, 1, 15, 22, 40, tzinfo=UTC),
            execution_client=_FakeExecution(position_size=0.05, position_value=5.0),
        )
    except RuntimeError as exc:
        assert "--i-understand-demo-flatten" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("demo flatten should require confirmation")


def test_demo_flatten_submits_reduce_only_market_orders(tmp_path: Path) -> None:
    execution = _FakeExecution(position_size=0.05, position_value=5.0)

    payload = run_bybit_demo_flatten(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        flatten_config=DemoFlattenConfig(confirmed=True),
        now=datetime(2026, 1, 15, 22, 40, tzinfo=UTC),
        execution_client=execution,
    )

    assert payload["rows"]["flatten_submitted"] == 1
    assert execution.placed[0]["orderType"] == "Market"
    assert execution.placed[0]["side"] == "Buy"
    assert execution.placed[0]["reduceOnly"] is True
    assert execution.placed[0]["orderLinkId"].startswith("agcflat")
    assert (tmp_path / "reports" / "bybit_demo_flatten_report.md").exists()


def _write_paper_trade(
    tmp_path: Path,
    *,
    status: str,
    trade_id: str = "paper-1",
    symbol: str = "BTCUSDT",
    exit_price: float | None = None,
) -> None:
    _write_paper_trades(tmp_path, [_paper_trade(status=status, trade_id=trade_id, symbol=symbol, exit_price=exit_price)])


def _write_paper_trades(tmp_path: Path, rows: list[dict]) -> None:
    write_dataset(
        pl.DataFrame(rows),
        tmp_path,
        "forward_paper_trades",
        partition_by=("date", "symbol"),
        append=False,
    )


def _write_paper_slices(tmp_path: Path, rows: list[dict]) -> None:
    write_dataset(
        pl.DataFrame(rows),
        tmp_path,
        "forward_paper_slices",
        partition_by=("date", "symbol"),
        append=False,
    )


def _paper_trade(
    *,
    status: str,
    trade_id: str = "paper-1",
    symbol: str = "BTCUSDT",
    exit_price: float | None = None,
    weight: float = 0.20,
    target_notional: float = 2_000.0,
    entry_twap_minutes: int = 0,
) -> dict:
    return {
        "trade_id": trade_id,
        "basket_id": "basket-1",
        "status": status,
        "symbol": symbol,
        "side": "short",
        "date": "2026-01-15",
        "entry_ts_ms": int(datetime(2026, 1, 15, 22, 16, tzinfo=UTC).timestamp() * 1000),
        "entry_price": 100.0,
        "mark_price": 99.5,
        "exit_price": exit_price,
        "exit_reason": "max_hold" if status == "closed" else "open",
        "actual_notional": target_notional,
        "target_notional": target_notional,
        "weight": weight,
        "target_weight": weight,
        "account_equity": 10_000.0,
        "entry_twap_minutes": entry_twap_minutes,
    }


def _paper_slice(slice_index: int, scheduled: datetime, *, status: str = "filled") -> dict:
    return {
        "trade_id": "paper-1",
        "basket_id": "basket-1",
        "symbol": "BTCUSDT",
        "date": "2026-01-15",
        "side": "short",
        "signal_ts_ms": int(datetime(2026, 1, 15, 22, 0, tzinfo=UTC).timestamp() * 1000),
        "signal_time": "2026-01-15T22:00:00+00:00",
        "paper_status": "open",
        "paper_entry_price": 100.0,
        "paper_exit_price": None,
        "paper_exit_reason": "open",
        "target_notional": 6_000.0,
        "actual_notional": 200.0,
        "entry_twap_minutes": 60,
        "slice_index": slice_index,
        "scheduled_ts_ms": int(scheduled.timestamp() * 1000),
        "scheduled_time": scheduled.isoformat(),
        "fill_ts_ms": int(scheduled.timestamp() * 1000) if status == "filled" else None,
        "fill_time": scheduled.isoformat() if status == "filled" else "",
        "status": status,
        "fill_price": 100.0,
        "avg_entry_price": 100.0,
        "stop_price": 120.0,
    }


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
            },
            {
                "symbol": "ETHUSDT",
                "status": "Trading",
                "priceFilter": {"tickSize": "0.1"},
                "lotSizeFilter": {
                    "qtyStep": "0.001",
                    "minOrderQty": "0.001",
                    "minNotionalValue": "5",
                },
            },
        ]

    def get_orderbook(self, symbol: str, limit: int = 1) -> dict:
        del symbol, limit
        return {"b": [["100", "1"]], "a": [["101", "1"]]}


class _FakeExpensiveMarket(_FakeMarket):
    def get_orderbook(self, symbol: str, limit: int = 1) -> dict:
        del symbol, limit
        return {"b": [["78000", "1"]], "a": [["78001", "1"]]}


class _FakeMissingMarket(_FakeMarket):
    def get_instruments_info(self) -> list[dict]:
        return []


class _FakeExecution:
    def __init__(
        self,
        *,
        position_size: float = 0.0,
        position_value: float = 0.0,
        open_order_link_id: str = "",
        fail_place_times: int = 0,
        fail_set_leverage: bool = False,
        wallet_equity: float = 10_000.0,
        position_idx: int = 0,
    ) -> None:
        self.placed: list[dict] = []
        self.place_attempts: list[dict] = []
        self.cancelled: list[dict] = []
        self.cancel_all: list[dict] = []
        self.leverage_calls: list[dict] = []
        self.position_size = position_size
        self.position_value = position_value
        self.open_order_link_id = open_order_link_id
        self.fail_place_times = fail_place_times
        self.fail_set_leverage = fail_set_leverage
        self.wallet_equity = wallet_equity
        self.position_idx = position_idx

    def get_wallet_balance(self, *, account_type: str, coin: str) -> dict:
        return {
            "accountType": account_type,
            "list": [
                {
                    "totalEquity": str(self.wallet_equity),
                    "coin": [{"coin": coin, "equity": str(self.wallet_equity)}],
                }
            ],
        }

    def set_leverage(self, *, symbol: str, buy_leverage: float, sell_leverage: float) -> dict:
        self.leverage_calls.append({"symbol": symbol, "buy_leverage": buy_leverage, "sell_leverage": sell_leverage})
        if self.fail_set_leverage:
            raise RuntimeError("simulated leverage failure")
        return {"symbol": symbol, "buyLeverage": str(buy_leverage), "sellLeverage": str(sell_leverage)}

    def place_order(self, **params):
        self.place_attempts.append(params)
        if len(self.place_attempts) <= self.fail_place_times:
            raise RuntimeError("simulated place failure")
        self.placed.append(params)
        return {"orderLinkId": params["orderLinkId"], "orderId": "demo-order"}

    def cancel_order(self, *, symbol: str, order_link_id: str):
        self.cancelled.append({"symbol": symbol, "order_link_id": order_link_id})
        return {"orderLinkId": order_link_id}

    def cancel_all_orders(self, *, symbol: str | None = None, settle_coin: str | None = None):
        self.cancel_all.append({"symbol": symbol, "settle_coin": settle_coin})
        return {"symbol": symbol, "settleCoin": settle_coin}

    def get_open_orders(self, *, symbol: str | None = None) -> list[dict]:
        if not self.open_order_link_id:
            return []
        return [{"symbol": symbol or "BTCUSDT", "orderLinkId": self.open_order_link_id, "orderStatus": "New"}]

    def get_positions(self, *, symbol: str | None = None, settle_coin: str | None = None) -> list[dict]:
        del settle_coin
        if self.position_size <= 0.0:
            return []
        return [
            {
                "symbol": symbol or "BTCUSDT",
                "side": "Sell",
                "size": str(self.position_size),
                "positionValue": str(self.position_value),
                "positionIdx": self.position_idx,
            }
        ]


class _CountingExecution(_FakeExecution):
    def __init__(self) -> None:
        super().__init__()
        self.private_reads = 0

    def get_open_orders(self, *, symbol: str | None = None) -> list[dict]:
        self.private_reads += 1
        return super().get_open_orders(symbol=symbol)

    def get_positions(self, *, symbol: str | None = None, settle_coin: str | None = None) -> list[dict]:
        self.private_reads += 1
        return super().get_positions(symbol=symbol, settle_coin=settle_coin)

    def get_order_history(self, *, symbol: str | None = None, order_link_id: str | None = None) -> list[dict]:
        del symbol, order_link_id
        self.private_reads += 1
        return []

    def get_trade_history(self, *, symbol: str | None = None, order_link_id: str | None = None) -> list[dict]:
        del symbol, order_link_id
        self.private_reads += 1
        return []


class _FilledOrderWithoutTradesExecution(_FakeExecution):
    def __init__(self) -> None:
        super().__init__()
        self.order_history_calls = 0
        self.trade_history_calls = 0

    def get_order_history(self, *, symbol: str | None = None, order_link_id: str | None = None) -> list[dict]:
        del symbol
        self.order_history_calls += 1
        return [{"orderLinkId": order_link_id, "orderStatus": "Filled"}]

    def get_trade_history(self, *, symbol: str | None = None, order_link_id: str | None = None) -> list[dict]:
        del symbol, order_link_id
        self.trade_history_calls += 1
        return []
