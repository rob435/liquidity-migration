from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.account_execution_stream import BybitAccountExecutionConsumer
from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.deterministic_runtime import VirtualClock


def _command(tmp_path: Path) -> tuple[AccountExecutionKernel, str]:
    clock = VirtualClock(current_wall_ns=1_100_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="stream-account", clock=clock, id_seed="stream-test")
    result = kernel.submit_targets(
        batch_id="batch-1",
        market_inputs=[MarketInputRef(
            input_key="book-1",
            symbol="BUSDT",
            exchange_ts_ns=900_000_000,
            local_receive_ts_ns=1_000_000_000,
            reference_price=10.0,
        )],
        targets=[DesiredTarget(
            decision_key="d1",
            target_key="long/main/BUSDT",
            sleeve="long",
            strategy_id="long-v1",
            component_id="main",
            symbol="BUSDT",
            signed_qty=2.0,
            reference_price=10.0,
            leverage=10.0,
        )],
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet", 950_000_000),
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 20.0, 10.0),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
    )
    return kernel, result.commands[0].command_id


def _execution(command_id: str, *, exec_id: str = "exec-1", qty: str = "1") -> dict[str, object]:
    return {"data": [{
        "orderLinkId": command_id,
        "orderId": "venue-1",
        "execId": exec_id,
        "execQty": qty,
        "execPrice": "10.1",
        "execFee": "0.001",
        "execTime": "1200",
        "side": "Buy",
        "seq": "7",
    }]}


def test_execution_before_create_ack_infers_ack_then_records_fill(tmp_path: Path) -> None:
    kernel, command_id = _command(tmp_path)
    consumer = BybitAccountExecutionConsumer(kernel=kernel)
    consumer.on_execution(_execution(command_id), local_receive_ts_ns=1_210_000_000)
    state = kernel.state()
    assert state.orders[command_id].status == "partially_filled"
    assert state.orders[command_id].venue_order_id == "venue-1"
    assert state.positions["BUSDT"].signed_qty == pytest.approx(1.0)
    assert state.executions["exec-1"]["metadata"]["source"] == "bybit_private_execution_ws"


def test_missing_execution_fee_is_persisted_as_pending_not_final_zero(tmp_path: Path) -> None:
    kernel, command_id = _command(tmp_path)
    message = _execution(command_id)
    del message["data"][0]["execFee"]  # type: ignore[index]

    BybitAccountExecutionConsumer(kernel=kernel).on_execution(
        message,
        local_receive_ts_ns=1_210_000_000,
    )

    fill = kernel.state().executions["exec-1"]
    assert fill["fee_usdt"] == 0.0
    assert fill["metadata"]["fee_observed"] is False
    assert fill["metadata"]["fee_status"] == "pending_missing_execution_fee"
    assert fill["metadata"]["fee_source"] == "bybit_private_execution.execFee"


def test_partial_cancel_waits_for_racing_execution_then_removes_phantom_remainder(tmp_path: Path) -> None:
    kernel, command_id = _command(tmp_path)
    kernel.record_ack(
        command_id=command_id,
        accepted=True,
        venue_order_id="venue-1",
        exchange_ts_ns=1_150_000_000,
        local_ack_ts_ns=1_151_000_000,
    )
    consumer = BybitAccountExecutionConsumer(kernel=kernel)
    consumer.on_order({"data": [{
        "orderLinkId": command_id,
        "orderStatus": "PartiallyFilledCanceled",
        "cumExecQty": "1",
        "updatedTime": "1210",
    }]}, local_receive_ts_ns=1_211_000_000)
    assert command_id in consumer.pending_terminal
    assert kernel.state().orders[command_id].status == "acknowledged"

    consumer.on_execution(_execution(command_id), local_receive_ts_ns=1_212_000_000)
    state = kernel.state()
    assert command_id not in consumer.pending_terminal
    assert state.orders[command_id].status == "partially_filled_cancelled"
    assert state.working_signed_qty("BUSDT") == 0.0


def test_terminal_cancel_before_create_response_infers_acceptance(tmp_path: Path) -> None:
    kernel, command_id = _command(tmp_path)
    consumer = BybitAccountExecutionConsumer(kernel=kernel)
    consumer.on_order({"data": [{
        "orderLinkId": command_id,
        "orderStatus": "Cancelled",
        "cumExecQty": "0",
        "updatedTime": "1210",
    }]}, local_receive_ts_ns=1_211_000_000)
    state = kernel.state()
    assert state.orders[command_id].status == "cancelled"
    assert state.orders[command_id].terminal_status_recorded
    assert state.working_signed_qty("BUSDT") == 0.0
    assert command_id not in consumer.pending_terminal


def test_terminal_reject_after_lost_response_removes_working_exposure(tmp_path: Path) -> None:
    kernel, command_id = _command(tmp_path)
    consumer = BybitAccountExecutionConsumer(kernel=kernel)
    consumer.on_order({"data": [{
        "orderLinkId": command_id,
        "orderStatus": "Rejected",
        "rejectReason": "EC_NoEnoughBalance",
        "cumExecQty": "0",
        "updatedTime": "1210",
    }]}, local_receive_ts_ns=1_211_000_000)

    state = kernel.state()
    assert state.orders[command_id].status == "rejected"
    assert state.orders[command_id].terminal_status_recorded
    assert state.working_signed_qty("BUSDT") == 0.0
    assert command_id not in consumer.pending_terminal


def test_duplicate_execution_and_terminal_messages_are_idempotent(tmp_path: Path) -> None:
    kernel, command_id = _command(tmp_path)
    consumer = BybitAccountExecutionConsumer(kernel=kernel)
    message = _execution(command_id, qty="2")
    consumer.on_execution(message, local_receive_ts_ns=1_210_000_000)
    consumer.on_execution(message, local_receive_ts_ns=1_210_000_000)
    terminal = {"data": [{
        "orderLinkId": command_id,
        "orderStatus": "Filled",
        "cumExecQty": "2",
        "updatedTime": "1210",
    }]}
    consumer.on_order(terminal, local_receive_ts_ns=1_211_000_000)
    consumer.on_order(terminal, local_receive_ts_ns=1_211_000_000)
    state = kernel.state()
    assert len(state.executions) == 1
    assert state.positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert state.orders[command_id].status == "filled"
