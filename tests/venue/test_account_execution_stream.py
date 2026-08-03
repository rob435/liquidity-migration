from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from liquidity_migration.venue.account_execution_stream import (
    BybitAccountExecutionConsumer,
    PrivateExecutionStreamSupervisor,
    _command_id_for_row,
)
from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.core.deterministic_runtime import VirtualClock


class _PrivateStream:
    def __init__(
        self,
        connected: bool | None,
        *,
        fail_order_subscription: bool = False,
        subscription_started: threading.Event | None = None,
        subscription_release: threading.Event | None = None,
    ) -> None:
        self.connected = connected
        self.fail_order_subscription = fail_order_subscription
        self.subscription_started = subscription_started
        self.subscription_release = subscription_release
        self.execution_subscriptions = 0
        self.order_subscriptions = 0
        self.close_calls = 0

    def is_connected(self) -> bool | None:
        return self.connected

    def subscribe_executions(self, _callback) -> None:
        self.execution_subscriptions += 1
        if self.subscription_started is not None:
            self.subscription_started.set()
        if self.subscription_release is not None:
            self.subscription_release.wait(timeout=2.0)

    def subscribe_orders(self, _callback) -> None:
        self.order_subscriptions += 1
        if self.fail_order_subscription:
            raise RuntimeError("order subscription failed")

    def close(self) -> None:
        self.close_calls += 1


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
    return {"creationTime": "1201", "data": [{
        "orderLinkId": command_id,
        "orderId": "venue-1",
        "execId": exec_id,
        "execQty": qty,
        "execPrice": "10.1",
        "execFee": "0.001",
        "feeRate": "0.00055",
        "feeCurrency": "USDT",
        "isMaker": False,
        "execType": "Trade",
        "execValue": "10.1",
        "orderQty": "2",
        "leavesQty": "1",
        "closedSize": "0",
        "orderType": "Market",
        "createType": "CreateByUser",
        "execTime": "1200",
        "side": "Buy",
        "seq": "7",
    }]}


def _started_consumer(tmp_path: Path, stream: _PrivateStream) -> BybitAccountExecutionConsumer:
    kernel, _command_id = _command(tmp_path)
    consumer = BybitAccountExecutionConsumer(kernel=kernel, private_stream=stream)
    consumer.start()
    return consumer


def _poll_supervisor(
    supervisor: PrivateExecutionStreamSupervisor,
    *,
    now_monotonic: float,
    until,
) -> bool | None:
    deadline = time.monotonic() + 2.0
    status: bool | None = None
    while time.monotonic() < deadline:
        status = supervisor.check(now_monotonic=now_monotonic)
        if until(status):
            return status
        time.sleep(0.005)
    pytest.fail("private stream supervisor did not settle before the test deadline")


def test_private_stream_supervisor_blocks_then_rebuilds_and_resubscribes(tmp_path: Path) -> None:
    old_stream = _PrivateStream(False)
    replacement = _PrivateStream(True)
    consumer = _started_consumer(tmp_path, old_stream)
    supervisor = PrivateExecutionStreamSupervisor(
        consumer=consumer,
        stream_factory=lambda: replacement,
        reconnect_after_seconds=180.0,
        reconnect_cooldown_seconds=180.0,
    )
    try:
        assert supervisor.check(now_monotonic=10.0) is False
        with pytest.raises(RuntimeError, match="private execution websocket is disconnected"):
            supervisor.require_recent_healthy(max_age_ns=1)
        assert supervisor.check(now_monotonic=189.9) is False
        assert supervisor.check(now_monotonic=190.1) is False
        assert _poll_supervisor(
            supervisor,
            now_monotonic=190.1,
            until=lambda status: status is True,
        ) is True
        assert consumer.private_stream is replacement
        assert old_stream.close_calls == 1
        assert replacement.execution_subscriptions == 1
        assert replacement.order_subscriptions == 1
        supervisor.require_recent_healthy(max_age_ns=1)
    finally:
        consumer.close()


def test_private_stream_supervisor_never_rebuilds_quiet_connected_socket(tmp_path: Path) -> None:
    stream = _PrivateStream(True)
    consumer = _started_consumer(tmp_path, stream)
    factory_calls = 0

    def factory() -> _PrivateStream:
        nonlocal factory_calls
        factory_calls += 1
        return _PrivateStream(True)

    supervisor = PrivateExecutionStreamSupervisor(
        consumer=consumer,
        stream_factory=factory,
        reconnect_after_seconds=1.0,
        reconnect_cooldown_seconds=1.0,
    )
    try:
        assert supervisor.check(now_monotonic=0.0) is True
        assert supervisor.check(now_monotonic=10_000.0) is True
        assert factory_calls == 0
    finally:
        consumer.close()


def test_private_stream_admission_reprobes_instead_of_trusting_cached_health(
    tmp_path: Path,
) -> None:
    stream = _PrivateStream(True)
    consumer = _started_consumer(tmp_path, stream)
    supervisor = PrivateExecutionStreamSupervisor(
        consumer=consumer,
        stream_factory=lambda: _PrivateStream(True),
        reconnect_after_seconds=1.0,
        reconnect_cooldown_seconds=1.0,
    )
    try:
        assert supervisor.check(now_monotonic=0.0) is True
        stream.connected = False

        with pytest.raises(RuntimeError, match="private execution websocket is disconnected"):
            supervisor.require_recent_healthy(max_age_ns=1_000_000_000)
    finally:
        consumer.close()


def test_private_stream_supervisor_prefers_ack_readiness_over_socket_liveness(
    tmp_path: Path,
) -> None:
    class _SocketWithoutSubscriptions(_PrivateStream):
        readiness_detail = "private execution websocket awaits positive subscription ACKs: order"

        def is_ready(self) -> bool:
            return False

    stream = _SocketWithoutSubscriptions(True)
    consumer = _started_consumer(tmp_path, stream)
    supervisor = PrivateExecutionStreamSupervisor(
        consumer=consumer,
        stream_factory=lambda: _PrivateStream(True),
        reconnect_after_seconds=1.0,
        reconnect_cooldown_seconds=1.0,
    )
    try:
        assert supervisor.check(now_monotonic=0.0) is False
        assert "awaits positive subscription ACKs" in supervisor.health_detail
        with pytest.raises(RuntimeError, match="positive subscription ACKs"):
            supervisor.require_recent_healthy(max_age_ns=1_000_000_000)
    finally:
        consumer.close()


def test_private_stream_supervisor_retries_failed_subscription_after_cooldown(tmp_path: Path) -> None:
    old_stream = _PrivateStream(False)
    failed_stream = _PrivateStream(True, fail_order_subscription=True)
    replacement = _PrivateStream(True)
    replacements = iter((failed_stream, replacement))
    consumer = _started_consumer(tmp_path, old_stream)
    supervisor = PrivateExecutionStreamSupervisor(
        consumer=consumer,
        stream_factory=lambda: next(replacements),
        reconnect_after_seconds=5.0,
        reconnect_cooldown_seconds=5.0,
    )
    try:
        assert supervisor.check(now_monotonic=0.0) is False
        assert supervisor.check(now_monotonic=5.1) is False
        _poll_supervisor(
            supervisor,
            now_monotonic=5.1,
            until=lambda _status: bool(supervisor.last_error),
        )
        assert supervisor.reconnect_attempts == 1
        assert "order subscription failed" in supervisor.last_error
        assert failed_stream.close_calls == 1
        assert consumer.private_stream is old_stream
        assert supervisor.check(now_monotonic=9.9) is False
        assert supervisor.reconnect_attempts == 1
        assert supervisor.check(now_monotonic=10.2) is False
        assert _poll_supervisor(
            supervisor,
            now_monotonic=10.2,
            until=lambda status: status is True,
        ) is True
        assert supervisor.reconnect_attempts == 2
        assert supervisor.reconnect_successes == 1
        assert consumer.private_stream is replacement
    finally:
        consumer.close()


def test_private_stream_supervisor_keeps_old_stream_when_candidate_never_ready(
    tmp_path: Path,
) -> None:
    old_stream = _PrivateStream(False)
    candidate = _PrivateStream(False)
    consumer = _started_consumer(tmp_path, old_stream)
    supervisor = PrivateExecutionStreamSupervisor(
        consumer=consumer,
        stream_factory=lambda: candidate,
        reconnect_after_seconds=1.0,
        reconnect_cooldown_seconds=1.0,
        candidate_ready_timeout_seconds=0.03,
        candidate_ready_poll_seconds=0.005,
    )
    try:
        assert supervisor.check(now_monotonic=0.0) is False
        assert supervisor.check(now_monotonic=1.1) is False
        _poll_supervisor(
            supervisor,
            now_monotonic=1.1,
            until=lambda _status: bool(supervisor.last_error),
        )

        assert "did not become ready" in supervisor.last_error
        assert consumer.private_stream is old_stream
        assert old_stream.close_calls == 0
        assert candidate.close_calls == 1
        assert supervisor.reconnect_successes == 0
    finally:
        consumer.close()


def test_private_stream_supervisor_discards_candidate_when_old_stream_recovers(
    tmp_path: Path,
) -> None:
    old_stream = _PrivateStream(False)
    candidate = _PrivateStream(False)
    consumer = _started_consumer(tmp_path, old_stream)
    supervisor = PrivateExecutionStreamSupervisor(
        consumer=consumer,
        stream_factory=lambda: candidate,
        reconnect_after_seconds=1.0,
        reconnect_cooldown_seconds=1.0,
        candidate_ready_timeout_seconds=0.5,
        candidate_ready_poll_seconds=0.005,
    )
    try:
        assert supervisor.check(now_monotonic=0.0) is False
        assert supervisor.check(now_monotonic=1.1) is False
        deadline = time.monotonic() + 1.0
        while candidate.order_subscriptions == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert candidate.order_subscriptions == 1

        old_stream.connected = True
        assert _poll_supervisor(
            supervisor,
            now_monotonic=1.2,
            until=lambda status: status is True and not supervisor.rebuild_in_progress,
        ) is True

        assert consumer.private_stream is old_stream
        assert old_stream.close_calls == 0
        assert candidate.close_calls == 1
        assert supervisor.reconnect_successes == 0
    finally:
        consumer.close()


def test_private_stream_rebuild_does_not_block_owner_during_subscription(
    tmp_path: Path,
) -> None:
    old_stream = _PrivateStream(False)
    subscription_started = threading.Event()
    subscription_release = threading.Event()
    replacement = _PrivateStream(
        True,
        subscription_started=subscription_started,
        subscription_release=subscription_release,
    )
    consumer = _started_consumer(tmp_path, old_stream)
    supervisor = PrivateExecutionStreamSupervisor(
        consumer=consumer,
        stream_factory=lambda: replacement,
        reconnect_after_seconds=1.0,
        reconnect_cooldown_seconds=1.0,
    )
    try:
        assert supervisor.check(now_monotonic=0.0) is False
        assert supervisor.check(now_monotonic=1.1) is False
        assert subscription_started.wait(timeout=1.0)

        started = time.monotonic()
        assert supervisor.check(now_monotonic=1.2) is False
        elapsed = time.monotonic() - started

        assert elapsed < 0.1
        assert supervisor.rebuild_in_progress
        assert "rebuild in progress" in supervisor.health_detail
    finally:
        subscription_release.set()
        consumer.close()


def test_private_stream_supervisor_unknown_liveness_blocks_without_rebuild(tmp_path: Path) -> None:
    stream = _PrivateStream(None)
    consumer = _started_consumer(tmp_path, stream)
    factory_calls = 0

    def factory() -> _PrivateStream:
        nonlocal factory_calls
        factory_calls += 1
        return _PrivateStream(True)

    supervisor = PrivateExecutionStreamSupervisor(
        consumer=consumer,
        stream_factory=factory,
        reconnect_after_seconds=1.0,
        reconnect_cooldown_seconds=1.0,
    )
    try:
        assert supervisor.check(now_monotonic=0.0) is None
        assert supervisor.check(now_monotonic=10_000.0) is None
        with pytest.raises(RuntimeError, match="liveness is unavailable"):
            supervisor.require_recent_healthy(max_age_ns=1)
        assert factory_calls == 0
    finally:
        consumer.close()


def test_execution_before_create_ack_infers_ack_then_records_fill(tmp_path: Path) -> None:
    kernel, command_id = _command(tmp_path)
    observed_executions: list[str] = []
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        fill_observer=observed_executions.append,
    )
    consumer.on_execution(_execution(command_id), local_receive_ts_ns=1_210_000_000)
    consumer.on_execution(_execution(command_id), local_receive_ts_ns=1_220_000_000)
    state = kernel.state()
    assert state.orders[command_id].status == "partially_filled"
    assert state.orders[command_id].venue_order_id == "venue-1"
    assert state.positions["BUSDT"].signed_qty == pytest.approx(1.0)
    metadata = state.executions["exec-1"]["metadata"]
    assert metadata["source"] == "bybit_private_execution_ws"
    assert metadata["is_maker"] is False
    assert metadata["fee_rate"] == "0.00055"
    assert metadata["fee_currency"] == "USDT"
    assert metadata["execution_type"] == "Trade"
    assert metadata["execution_value"] == "10.1"
    assert metadata["order_qty"] == "2"
    assert metadata["leaves_qty"] == "1"
    assert metadata["message_creation_ts_ns"] == 1_201_000_000
    assert observed_executions == ["exec-1"]


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


def test_malformed_known_command_execution_latches_health_failure(tmp_path: Path) -> None:
    """A fill for a KNOWN command whose qty/price cannot be parsed is missing
    accounting: it must latch a health-blocking error, never vanish silently."""

    kernel, command_id = _command(tmp_path)
    kernel.record_ack(
        command_id=command_id,
        accepted=True,
        venue_order_id="venue-malformed",
        exchange_ts_ns=1_150_000_000,
        local_ack_ts_ns=1_151_000_000,
    )
    message = _execution(command_id)
    message["data"][0]["execPrice"] = None  # type: ignore[index]

    class RecordingManager:
        def __init__(self) -> None:
            self.failures: list[tuple[dict, Exception]] = []

        def is_position_execution(self, row) -> bool:
            return True

        @staticmethod
        def has_native_stop_provenance(row) -> bool:
            return False

        @staticmethod
        def native_execution_identity_evidence(row) -> str:
            return ""

        def note_adoption_failure(self, row, exc) -> None:
            self.failures.append((dict(row), exc))

    manager = RecordingManager()
    BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,  # type: ignore[arg-type]
    ).on_execution(message, local_receive_ts_ns=1_210_000_000)

    assert "exec-1" not in kernel.state().executions
    assert len(manager.failures) == 1
    assert "malformed" in str(manager.failures[0][1])


def _extra_command(kernel: AccountExecutionKernel, *, batch_id: str, target_qty: float) -> str:
    result = kernel.submit_targets(
        batch_id=batch_id,
        market_inputs=[MarketInputRef(
            input_key=f"book-{batch_id}",
            symbol="BUSDT",
            exchange_ts_ns=900_000_000,
            local_receive_ts_ns=1_000_000_000,
            reference_price=10.0,
        )],
        targets=[DesiredTarget(
            decision_key=f"d-{batch_id}",
            target_key="hedge/main/BUSDT",
            sleeve="hedge",
            strategy_id="hedge-v1",
            component_id="main",
            symbol="BUSDT",
            signed_qty=target_qty,
            reference_price=10.0,
            leverage=10.0,
        )],
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet", 950_000_000),
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 20.0, 10.0),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
    )
    assert len(result.commands) == 1
    return result.commands[0].command_id


def test_row_without_a_link_id_joins_on_the_venue_order_id(tmp_path: Path) -> None:
    kernel, command_id = _command(tmp_path)
    kernel.record_ack(
        command_id=command_id,
        accepted=True,
        venue_order_id="venue-solo",
        exchange_ts_ns=1_150_000_000,
        local_ack_ts_ns=1_151_000_000,
    )

    row = {"orderId": "venue-solo"}
    assert _command_id_for_row(row, kernel._state_ref()) == command_id


def test_two_commands_sharing_a_venue_order_id_resolve_to_no_command(tmp_path: Path) -> None:
    kernel, first_command_id = _command(tmp_path)
    kernel.record_ack(
        command_id=first_command_id,
        accepted=True,
        venue_order_id="venue-shared",
        exchange_ts_ns=1_150_000_000,
        local_ack_ts_ns=1_151_000_000,
    )
    # Clear the working order so the next batch commands the same symbol again.
    kernel.record_order_status(
        command_id=first_command_id,
        status="cancelled",
        cumulative_filled_qty=0.0,
        exchange_ts_ns=1_152_000_000,
        local_receive_ts_ns=1_153_000_000,
    )
    second_command_id = _extra_command(kernel, batch_id="batch-2", target_qty=1.0)
    kernel.record_ack(
        command_id=second_command_id,
        accepted=True,
        venue_order_id="venue-shared",
        exchange_ts_ns=1_154_000_000,
        local_ack_ts_ns=1_155_000_000,
    )
    state = kernel._state_ref()
    assert state.command_ids_by_venue_order_id["venue-shared"] == {first_command_id, second_command_id}

    # The join is ambiguous, so venue identity resolves to nothing; only an
    # explicit client id can still name the command.
    assert _command_id_for_row({"orderId": "venue-shared"}, state) == ""
    assert _command_id_for_row(
        {"orderId": "venue-shared", "orderLinkId": second_command_id}, state
    ) == second_command_id

    message = _execution(first_command_id)
    message["data"][0]["orderId"] = "venue-shared"  # type: ignore[index]
    message["data"][0]["orderLinkId"] = ""  # type: ignore[index]
    BybitAccountExecutionConsumer(kernel=kernel).on_execution(
        message,
        local_receive_ts_ns=1_210_000_000,
    )
    assert kernel.state().executions == {}
