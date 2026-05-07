from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from aggression_carry.config import ResearchConfig
from aggression_carry.demo_fast_protection import DemoFastProtectionConfig, run_demo_fast_protection
from aggression_carry.storage import read_dataset, write_dataset


NOW = datetime(2026, 1, 15, 23, 16, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def test_fast_protection_does_not_activate_before_profit_time(tmp_path: Path) -> None:
    _write_open_trade(tmp_path, profit_active_ts_ms=NOW_MS + 60_000)
    _write_entry_order(tmp_path)
    _write_fast_state(tmp_path)
    stream = _FakeStream([_event(98.5)])

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(runtime_seconds=0, submit_exits=True, confirmed=True),
        now=NOW,
        execution_client=_FakeExecution(position_size=0.1, position_value=10.0),
        trade_stream=stream,
    )

    assert payload["reason"] == "no_active_profit_protected_trades"
    assert stream.subscriptions == []
    assert read_dataset(tmp_path, "demo_fast_protection_state").is_empty()


def test_fast_protection_submits_whole_symbol_reduce_only_exit_on_mfe_giveback(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    _write_entry_order(tmp_path)
    execution = _FakeExecution(position_size=0.1, position_value=10.0)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(
            runtime_seconds=0,
            submit_exits=True,
            confirmed=True,
            order_link_prefix="r31p",
        ),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(97.0), _event(97.7)]),
    )

    assert payload["rows"]["submitted_or_unknown"] == 1
    assert len(execution.placed) == 1
    assert execution.placed[0]["orderLinkId"] != "exit-cancelled"
    order = execution.placed[0]
    assert order["reduceOnly"] is True
    assert order["orderType"] == "Limit"
    assert order["timeInForce"] == "IOC"
    assert order["price"] == "98.1885"
    assert order["qty"] == "0.1"
    assert order["side"] == "Buy"
    orders = read_dataset(tmp_path, "demo_execution_orders")
    exit_row = orders.filter(pl.col("action") == "exit").row(0, named=True)
    assert exit_row["paper_exit_reason"] == "fast_mfe_giveback"
    assert exit_row["paper_exit_price"] == 97.6
    events = read_dataset(tmp_path, "demo_fast_protection_events")
    assert events.row(0, named=True)["model_exit_price"] == 97.6


def test_fast_protection_seeds_pre_activation_mfe_without_first_event_exit(tmp_path: Path) -> None:
    _write_open_trade(tmp_path, mfe=0.05, mark_price=95.0)
    _write_entry_order(tmp_path)
    execution = _FakeExecution(position_size=0.1, position_value=10.0)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(
            runtime_seconds=0,
            submit_exits=True,
            confirmed=True,
            order_link_prefix="r31p",
        ),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(98.5)]),
    )

    assert payload["rows"]["trigger_events"] == 0
    assert execution.placed == []
    state = read_dataset(tmp_path, "demo_fast_protection_state").row(0, named=True)
    assert state["best_price"] == 95.0
    assert state["has_active_observation"] is True
    assert state["mfe_giveback_active"] is True


def test_fast_protection_does_not_exit_inside_first_active_minute(tmp_path: Path) -> None:
    _write_open_trade(tmp_path, profit_active_ts_ms=NOW_MS, mfe=0.05, mark_price=95.0)
    _write_entry_order(tmp_path)
    execution = _FakeExecution(position_size=0.1, position_value=10.0)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(
            runtime_seconds=0,
            submit_exits=True,
            confirmed=True,
            order_link_prefix="r31p",
        ),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(98.5, ts_ms=NOW_MS + 1_000), _event(98.6, ts_ms=NOW_MS + 20_000)]),
    )

    assert payload["rows"]["trigger_events"] == 0
    assert execution.placed == []


def test_fast_protection_can_exit_after_first_active_minute_from_seeded_mfe(tmp_path: Path) -> None:
    _write_open_trade(tmp_path, profit_active_ts_ms=NOW_MS - 60_000, mfe=0.05, mark_price=95.0)
    _write_entry_order(tmp_path)
    execution = _FakeExecution(position_size=0.1, position_value=10.0)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(
            runtime_seconds=0,
            submit_exits=True,
            confirmed=True,
            order_link_prefix="r31p",
        ),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(98.5, ts_ms=NOW_MS), _event(98.6, ts_ms=NOW_MS + 1_000)]),
    )

    assert payload["rows"]["submitted_or_unknown"] == 1
    assert len(execution.placed) == 1


def test_fast_protection_updates_mfe_event_by_event_before_giveback(tmp_path: Path) -> None:
    _write_open_trade(tmp_path, mfe=0.0, mark_price=100.0)
    _write_entry_order(tmp_path)
    execution = _FakeExecution(position_size=0.1, position_value=10.0)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(
            runtime_seconds=0,
            submit_exits=True,
            confirmed=True,
            order_link_prefix="r31p",
        ),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(97.0), _event(97.7)]),
    )

    assert payload["rows"]["submitted_or_unknown"] == 1
    assert len(execution.placed) == 1
    orders = read_dataset(tmp_path, "demo_execution_orders")
    exit_row = orders.filter(pl.col("action") == "exit").row(0, named=True)
    state = read_dataset(tmp_path, "demo_fast_protection_state").row(0, named=True)
    assert exit_row["paper_exit_reason"] == "fast_mfe_giveback"
    assert state["best_price"] == 97.0
    assert state["mfe"] == 0.03


def test_fast_protection_cancels_open_entries_before_reduce_only_exit(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    _write_entry_order(tmp_path, reconciled_status="filled")
    execution = _FakeExecution(position_size=0.1, position_value=10.0, open_order_link_id="entry-1")

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(
            runtime_seconds=0,
            submit_exits=True,
            confirmed=True,
            order_link_prefix="r31p",
        ),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(97.0), _event(97.7)]),
    )

    assert payload["rows"]["submitted_or_unknown"] == 1
    assert execution.actions == ["cancel:entry-1", "place:Buy"]
    assert execution.placed[0]["reduceOnly"] is True


def test_fast_protection_retries_after_terminal_cancelled_exit(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    _write_entry_and_cancelled_exit_orders(tmp_path, reconciled_status="cancelled")
    execution = _FakeExecution(position_size=0.1, position_value=10.0)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(
            runtime_seconds=0,
            submit_exits=True,
            confirmed=True,
            order_link_prefix="r31p",
        ),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(97.0), _event(97.7)]),
    )

    assert payload["rows"]["submitted_or_unknown"] == 1
    assert len(execution.placed) == 1


def test_fast_protection_retries_after_partial_cancelled_exit(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    _write_entry_and_cancelled_exit_orders(tmp_path, reconciled_status="partial_cancelled")
    execution = _FakeExecution(position_size=0.07, position_value=7.0)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(
            runtime_seconds=0,
            submit_exits=True,
            confirmed=True,
            order_link_prefix="r31p",
        ),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(97.0), _event(97.7)]),
    )

    assert payload["rows"]["submitted_or_unknown"] == 1
    assert len(execution.placed) == 1
    assert execution.placed[0]["qty"] == "0.07"


def test_fast_protection_requires_filled_entry_exposure(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    _write_entry_order(tmp_path, reconciled_status="open_order_seen")
    stream = _FakeStream([_event(97.0), _event(97.7)])

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(runtime_seconds=0, submit_exits=True, confirmed=True),
        now=NOW,
        execution_client=_FakeExecution(position_size=0.1, position_value=10.0),
        trade_stream=stream,
    )

    assert payload["reason"] == "no_active_profit_protected_trades"
    assert stream.subscriptions == []


def test_fast_protection_blocks_duplicate_exit_under_burst_events(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    _write_entry_order(tmp_path)
    execution = _FakeExecution(position_size=0.1, position_value=10.0)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(runtime_seconds=0, submit_exits=True, confirmed=True),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(97.0), _event(97.7), _event(97.8)]),
    )

    assert len(execution.placed) == 1
    assert payload["rows"]["duplicate_blocks"] == 1


def test_fast_protection_retries_after_pre_submit_exception(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    _write_entry_order(tmp_path)
    execution = _FakeExecution(position_size=0.1, position_value=10.0, fail_position_calls=2)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(runtime_seconds=0, submit_exits=True, confirmed=True),
        now=NOW,
        execution_client=execution,
        trade_stream=_FakeStream([_event(97.0), _event(97.7), _event(97.8)]),
    )

    assert len(execution.placed) == 1
    assert payload["rows"]["trigger_events"] == 2
    assert payload["rows"]["submitted_or_unknown"] == 1
    assert payload["rows"]["submit_blocks"] == 1
    events = read_dataset(tmp_path, "demo_fast_protection_events").sort("price")
    assert events["result_status"].to_list() == ["submit_exception", "accepted"]
    state = read_dataset(tmp_path, "demo_fast_protection_state").row(0, named=True)
    assert state["exit_status"] == "accepted"
    assert state["exit_order_link_id"]


def test_fast_protection_observe_only_makes_no_private_calls(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    _write_entry_order(tmp_path)

    payload = run_demo_fast_protection(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
        protection_config=DemoFastProtectionConfig(runtime_seconds=0, submit_exits=False, confirmed=False),
        now=NOW,
        trade_stream=_FakeStream([_event(97.0), _event(97.7)]),
    )

    assert payload["rows"]["trigger_events"] == 1
    assert payload["rows"]["submitted_or_unknown"] == 0
    orders = read_dataset(tmp_path, "demo_execution_orders")
    assert orders.filter(pl.col("action") == "exit").is_empty()


def test_fast_protection_submit_requires_confirmation(tmp_path: Path) -> None:
    try:
        run_demo_fast_protection(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            fade_config=ResearchConfig(data_root=tmp_path).daily_close_fade,
            protection_config=DemoFastProtectionConfig(runtime_seconds=0, submit_exits=True, confirmed=False),
            now=NOW,
            trade_stream=_FakeStream([]),
        )
    except RuntimeError as exc:
        assert "confirmation" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("fast protection submit mode should require confirmation")


def _write_open_trade(
    data_root: Path,
    *,
    profit_active_ts_ms: int = NOW_MS - 60_000,
    mfe: float = 0.02,
    mark_price: float = 98.0,
) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "status": "open",
                    "symbol": "BTCUSDT",
                    "side": "short",
                    "date": "2026-01-15",
                    "entry_ts_ms": NOW_MS - 75 * 60_000,
                    "entry_price": 100.0,
                    "avg_entry_price": 100.0,
                    "mark_price": mark_price,
                    "mfe": mfe,
                    "realized_vol": 0.01,
                    "vol_trailing_stop_mult": 0.25,
                    "vol_trailing_activation_mult": 0.0,
                    "mfe_giveback_activation_pct": 0.01,
                    "mfe_giveback_pct": 0.20,
                    "profit_protection_active_ts_ms": profit_active_ts_ms,
                    "exit_price": None,
                    "exit_reason": "open",
                    "actual_notional": 2_000.0,
                    "target_notional": 2_000.0,
                }
            ],
            infer_schema_length=None,
        ),
        data_root,
        "forward_paper_trades",
        partition_by=("date", "symbol"),
        append=False,
    )


def _write_entry_order(data_root: Path, *, reconciled_status: str = "filled") -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "entry-1",
                    "order_id": "demo-entry",
                    "paper_trade_id": "paper-1",
                    "basket_id": "basket-1",
                    "date": "2026-01-15",
                    "action": "entry",
                    "status": "accepted",
                    "reconciled_status": reconciled_status,
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "order_type": "Limit",
                    "time_in_force": "PostOnly",
                    "qty": "0.05",
                    "price": "100",
                    "reduce_only": False,
                    "estimated_notional": 5.0,
                    "max_order_notional": 5.0,
                    "created_ts_ms": NOW_MS - 60_000,
                    "created_time": datetime.fromtimestamp((NOW_MS - 60_000) / 1000, tz=UTC).isoformat(),
                }
            ],
            infer_schema_length=None,
        ),
        data_root,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )


def _write_entry_and_cancelled_exit_orders(data_root: Path, *, reconciled_status: str) -> None:
    entry = {
        "order_link_id": "entry-1",
        "order_id": "demo-entry",
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
        "qty": "0.05",
        "price": "100",
        "reduce_only": False,
        "estimated_notional": 5.0,
        "max_order_notional": 5.0,
        "created_ts_ms": NOW_MS - 60_000,
        "created_time": datetime.fromtimestamp((NOW_MS - 60_000) / 1000, tz=UTC).isoformat(),
    }
    exit_row = {
        **entry,
        "order_link_id": "exit-cancelled",
        "order_id": "demo-exit",
        "action": "exit",
        "status": "accepted",
        "reconciled_status": reconciled_status,
        "side": "Buy",
        "order_type": "Market",
        "time_in_force": "IOC",
        "reduce_only": True,
        "created_ts_ms": NOW_MS - 30_000,
        "created_time": datetime.fromtimestamp((NOW_MS - 30_000) / 1000, tz=UTC).isoformat(),
    }
    write_dataset(
        pl.DataFrame([entry, exit_row], infer_schema_length=None),
        data_root,
        "demo_execution_orders",
        partition_by=("date", "symbol"),
        append=False,
    )


def _write_fast_state(data_root: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "paper_trade_id": "old-paper",
                    "symbol": "OLDUSDT",
                    "date": "2026-01-14",
                    "updated_ts_ms": NOW_MS - 86_400_000,
                    "best_price": 1.0,
                    "exit_status": "accepted",
                }
            ],
            infer_schema_length=None,
        ),
        data_root,
        "demo_fast_protection_state",
        partition_by=("date", "symbol"),
        append=False,
    )


def _event(price: float, *, ts_ms: int = NOW_MS) -> dict[str, Any]:
    return {"data": [{"s": "BTCUSDT", "p": str(price), "T": ts_ms}]}


class _FakeStream:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.subscriptions: list[list[str]] = []
        self.closed = False

    def subscribe_public_trades(self, symbols: list[str], callback: Any) -> None:
        self.subscriptions.append(symbols)
        for event in self.events:
            callback(event)

    def close(self) -> None:
        self.closed = True


class _FakeExecution:
    def __init__(
        self,
        *,
        position_size: float,
        position_value: float,
        open_order_link_id: str = "",
        fail_position_calls: int = 0,
    ) -> None:
        self.position_size = position_size
        self.position_value = position_value
        self.open_order_link_id = open_order_link_id
        self.fail_position_calls = fail_position_calls
        self.placed: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self.actions: list[str] = []

    def get_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        if not self.open_order_link_id:
            return []
        return [{"symbol": symbol or "BTCUSDT", "orderLinkId": self.open_order_link_id, "orderStatus": "New"}]

    def get_order_history(self, *, symbol: str | None = None, order_link_id: str | None = None) -> list[dict[str, Any]]:
        del symbol, order_link_id
        return []

    def get_trade_history(self, *, symbol: str | None = None, order_link_id: str | None = None) -> list[dict[str, Any]]:
        del symbol, order_link_id
        return []

    def get_positions(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.fail_position_calls > 0:
            self.fail_position_calls -= 1
            raise RuntimeError("transient position failure")
        return [
            {
                "symbol": symbol or "BTCUSDT",
                "side": "Sell",
                "size": str(self.position_size),
                "positionValue": str(self.position_value),
            }
        ]

    def place_order(self, **params: Any) -> dict[str, Any]:
        self.actions.append(f"place:{params.get('side')}")
        self.placed.append(params)
        return {"orderLinkId": params["orderLinkId"], "orderId": f"demo-{len(self.placed)}"}

    def cancel_order(self, *, symbol: str, order_link_id: str) -> dict[str, Any]:
        self.cancelled.append({"symbol": symbol, "order_link_id": order_link_id})
        self.actions.append(f"cancel:{order_link_id}")
        return {"orderLinkId": order_link_id}
