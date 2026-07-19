from __future__ import annotations

import threading
from pathlib import Path

import pytest

from liquidity_migration.account_execution_stream import BybitAccountExecutionConsumer
from liquidity_migration.account_kernel import (
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    TargetBatchResult,
    read_account_journal,
)
from liquidity_migration.account_service_bybit import inspect_bybit_demo_order_ownership
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.execution_adapters import ExecutionObservation, KernelExecutionDriver
from liquidity_migration.venue_protection import BybitNativeProtectionManager


class _DemoClient:
    demo = True

    def __init__(self) -> None:
        self.stops: list[dict[str, object]] = []

    def set_trading_stop(self, **params: object) -> dict[str, object]:
        self.stops.append(params)
        return {}


class _BlockingDemoClient(_DemoClient):
    def __init__(self) -> None:
        super().__init__()
        self.first_call_entered = threading.Event()
        self.release_first_call = threading.Event()
        self._calls_lock = threading.Lock()

    def set_trading_stop(self, **params: object) -> dict[str, object]:
        with self._calls_lock:
            self.stops.append(params)
            call_number = len(self.stops)
        if call_number == 1:
            self.first_call_entered.set()
            if not self.release_first_call.wait(timeout=5.0):
                raise TimeoutError("test did not release the first native-stop mutation")
        return {}


def _open_position(
    root: Path,
    *,
    signed_qty: float,
    fill_price: float = 10.0,
    metadata: dict[str, object] | None = None,
) -> tuple[AccountExecutionKernel, VirtualClock]:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(root, account_id="demo", clock=clock, id_seed="native")
    market = MarketInputRef(
        input_key="book-1",
        symbol="BUSDT",
        exchange_ts_ns=1_000_000_000,
        local_receive_ts_ns=1_100_000_000,
        reference_price=10.0,
    )
    result = kernel.submit_targets(
        batch_id="open",
        market_inputs=(market,),
        targets=(DesiredTarget(
            decision_key="open-d",
            target_key="long/strategy/trade/BUSDT" if signed_qty > 0 else "continuous/strategy/trade/BUSDT",
            sleeve="long" if signed_qty > 0 else "continuous",
            strategy_id="strategy",
            component_id="trade",
            symbol="BUSDT",
            signed_qty=signed_qty,
            reference_price=10.0,
            leverage=10.0,
            reason="entry",
            metadata=metadata or {},
        ),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 1_500_000_000),
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1),
        },
    )
    command = result.commands[0]
    KernelExecutionDriver(kernel).ingest((
        ExecutionObservation(
            observation_type="ack",
            command_id=command.command_id,
            exchange_ts_ns=1_600_000_000,
            local_receive_ts_ns=1_610_000_000,
            accepted=True,
            venue_order_id="entry-order",
        ),
        ExecutionObservation(
            observation_type="fill",
            command_id=command.command_id,
            exchange_ts_ns=1_620_000_000,
            local_receive_ts_ns=1_625_000_000,
            venue_order_id="entry-order",
            execution_id="entry-fill",
            signed_qty=signed_qty,
            price=fill_price,
            fee_usdt=0.01,
        ),
    ))
    return kernel, clock


def _manager(kernel: AccountExecutionKernel, clock: VirtualClock) -> tuple[BybitNativeProtectionManager, _DemoClient]:
    client = _DemoClient()
    manager = BybitNativeProtectionManager(
        kernel=kernel,
        client=client,
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT", 0.1, 0.1, 1.0, tick_size=0.1, environment="demo"
            ),
        },
        fallback_stop_fraction=0.07,
        clock=clock,
    )
    return manager, client


def _replace_long_target(
    kernel: AccountExecutionKernel,
    *,
    batch_id: str,
    signed_qty: float,
) -> TargetBatchResult:
    return kernel.submit_targets(
        batch_id=batch_id,
        market_inputs=(MarketInputRef(
            input_key=f"book-{batch_id}",
            symbol="BUSDT",
            exchange_ts_ns=1_800_000_000,
            local_receive_ts_ns=1_810_000_000,
            reference_price=10.0,
        ),),
        targets=(DesiredTarget(
            decision_key=f"decision-{batch_id}",
            target_key="long/strategy/trade/BUSDT",
            sleeve="long",
            strategy_id="strategy",
            component_id="trade",
            symbol="BUSDT",
            signed_qty=signed_qty,
            reference_price=10.0,
            leverage=10.0,
            reason="test replacement",
        ),),
        risk_snapshot=AccountRiskSnapshot(
            10_000.0,
            9_000.0,
            f"wallet-{batch_id}",
            1_805_000_000,
        ),
        risk_policy=AccountRiskPolicy(
            1_000.0,
            1_000.0,
            1_000.0,
            1_000.0,
            10.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1),
        },
    )


def test_manager_retains_existing_stop_during_fully_covered_canonical_close(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=2.0)
    manager, client = _manager(kernel, clock)
    installed = manager.sync("BUSDT")
    assert installed is not None

    close = _replace_long_target(kernel, batch_id="close", signed_qty=0.0)
    assert len(close.commands) == 1
    assert close.commands[0].reduce_only is True
    retained = manager.sync("BUSDT")

    assert retained is not None
    assert retained.protection_key == installed.protection_key
    assert retained.stop_price == installed.stop_price
    assert len(client.stops) == 1
    manager.require_recent_healthy(max_age_ns=1_000_000_000)

    command = close.commands[0]
    KernelExecutionDriver(kernel).ingest((
        ExecutionObservation(
            observation_type="ack",
            command_id=command.command_id,
            exchange_ts_ns=1_820_000_000,
            local_receive_ts_ns=1_821_000_000,
            accepted=True,
            venue_order_id="close-order",
        ),
        ExecutionObservation(
            observation_type="fill",
            command_id=command.command_id,
            exchange_ts_ns=1_830_000_000,
            local_receive_ts_ns=1_831_000_000,
            venue_order_id="close-order",
            execution_id="close-partial",
            signed_qty=-0.5,
            price=10.0,
            fee_usdt=0.01,
        ),
    ))
    partial = manager.sync("BUSDT")
    assert partial is not None
    assert partial.protection_key == installed.protection_key
    assert partial.signed_qty == 1.5
    assert len(client.stops) == 1

    KernelExecutionDriver(kernel).ingest((ExecutionObservation(
        observation_type="fill",
        command_id=command.command_id,
        exchange_ts_ns=1_840_000_000,
        local_receive_ts_ns=1_841_000_000,
        venue_order_id="close-order",
        execution_id="close-final",
        signed_qty=-1.5,
        price=10.0,
        fee_usdt=0.01,
    ),))
    assert manager.sync("BUSDT") is None
    assert manager.active("BUSDT") is None
    assert kernel.state().protections[installed.protection_key]["status"] == "position_flat"


def test_manager_does_not_invent_stop_for_unprotected_close_transition(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=2.0)
    close = _replace_long_target(kernel, batch_id="close", signed_qty=0.0)
    assert close.commands[0].reduce_only is True
    manager, client = _manager(kernel, clock)

    with pytest.raises(RuntimeError, match="no same-direction component target owner"):
        manager.plan("BUSDT")
    assert client.stops == []


def test_manager_rejects_superseded_risk_increasing_work_as_close_coverage(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=2.0)
    manager, _client = _manager(kernel, clock)
    manager.sync("BUSDT")
    increase = _replace_long_target(kernel, batch_id="increase", signed_qty=3.0)
    assert len(increase.commands) == 1
    assert increase.commands[0].reduce_only is False
    close = _replace_long_target(kernel, batch_id="supersede-flat", signed_qty=0.0)
    assert close.accepted is True
    assert close.commands == ()

    with pytest.raises(RuntimeError, match="no same-direction component target owner"):
        manager.plan("BUSDT")


def test_manager_rejects_terminal_close_as_position_coverage(tmp_path: Path) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=2.0)
    manager, _client = _manager(kernel, clock)
    manager.sync("BUSDT")
    close = _replace_long_target(kernel, batch_id="close", signed_qty=0.0)
    command = close.commands[0]
    KernelExecutionDriver(kernel).ingest((ExecutionObservation(
        observation_type="ack",
        command_id=command.command_id,
        exchange_ts_ns=1_820_000_000,
        local_receive_ts_ns=1_821_000_000,
        accepted=False,
        rejection_key="venue-rejected-close",
    ),))

    with pytest.raises(RuntimeError, match="no same-direction component target owner"):
        manager.plan("BUSDT")


def test_manager_installs_outermost_component_stop_and_requires_health(tmp_path: Path) -> None:
    kernel, clock = _open_position(
        tmp_path,
        signed_qty=2.0,
        fill_price=11.0,
        metadata={"stop_loss_pct": 0.125},
    )
    manager, client = _manager(kernel, clock)

    with pytest.raises(RuntimeError, match="lack active"):
        manager.require_recent_healthy(max_age_ns=1_000_000_000)
    plan = manager.sync("BUSDT")

    assert plan is not None
    # Decision reference was 10.0; confirmed fill VWAP was 11.0. A
    # decision-anchored stop would be 8.7 after tick rounding, while the causal
    # fill-anchored stop is floor(11 * (1 - 0.125), 0.1) = 9.6.
    assert plan.stop_price == 9.6
    assert plan.stop_source == "fill_anchored_outermost_component_stop"
    assert client.stops == [{
        "symbol": "BUSDT",
        "tpsl_mode": "Full",
        "position_idx": 0,
        "stop_loss": "9.6",
        "take_profit": "0",
        "sl_trigger_by": "MarkPrice",
        "tp_trigger_by": None,
    }]
    manager.require_recent_healthy(max_age_ns=1_000_000_000)

    clock.advance_ns(1_000_000_001)
    with pytest.raises(RuntimeError, match="stale"):
        manager.require_recent_healthy(max_age_ns=1_000_000_000)
    manager.reconcile_venue_positions([{
        "symbol": "BUSDT",
        "side": "Buy",
        "size": "2",
        "stopLoss": "9.6",
    }])
    manager.require_recent_healthy(max_age_ns=1_000_000_000)
    assert len(client.stops) == 1


def test_concurrent_sync_reconcile_and_order_binding_install_one_activation(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    client = _BlockingDemoClient()
    manager = BybitNativeProtectionManager(
        kernel=kernel,
        client=client,
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT", 0.1, 0.1, 1.0, tick_size=0.1, environment="demo"
            ),
        },
        fallback_stop_fraction=0.07,
        clock=clock,
    )
    failures: list[BaseException] = []
    binding_results: list[bool] = []
    start_contenders = threading.Barrier(3)

    def capture_failure(operation: object) -> None:
        try:
            assert callable(operation)
            operation()
        except BaseException as exc:  # noqa: BLE001 - retain thread failure for assertion
            failures.append(exc)

    installer = threading.Thread(
        target=capture_failure,
        args=(lambda: manager.sync("BUSDT"),),
    )
    installer.start()
    assert client.first_call_entered.wait(timeout=5.0)

    def reconcile() -> None:
        start_contenders.wait(timeout=5.0)
        manager.reconcile_venue_positions([{
            "symbol": "BUSDT",
            "side": "Sell",
            "size": "2",
            "stopLoss": "10.7",
        }])

    def bind_order() -> None:
        start_contenders.wait(timeout=5.0)
        binding_results.append(manager.observe_order({
            "symbol": "BUSDT",
            "orderLinkId": "",
            "orderId": "native-order-1",
            "orderStatus": "Triggered",
            "cumExecQty": "0",
            "triggerPrice": "10.7",
            "createType": "CreateByStopLoss",
            "stopOrderType": "StopLoss",
        }))

    reconciler = threading.Thread(target=capture_failure, args=(reconcile,))
    observer = threading.Thread(target=capture_failure, args=(bind_order,))
    reconciler.start()
    observer.start()
    start_contenders.wait(timeout=5.0)
    client.release_first_call.set()

    for thread in (installer, reconciler, observer):
        thread.join(timeout=5.0)
        assert not thread.is_alive()
    assert failures == []
    assert binding_results == [True]
    assert len(client.stops) == 1

    native = [
        protection
        for protection in kernel.state().protections.values()
        if (protection.get("metadata") or {}).get("native_exchange")
    ]
    assert len(native) == 1
    assert native[0]["metadata"]["activation_revision"] == 1
    assert manager.observed_native_order_ids == {"BUSDT": "native-order-1"}
    assert manager.native_execution_identity_evidence({
        "symbol": "BUSDT",
        "orderId": "native-order-1",
    }) == "matched_verified_native_order_event"


@pytest.mark.parametrize(
    (
        "signed_qty",
        "first_stop_loss_pct",
        "second_stop_loss_pct",
        "expected_stop",
    ),
    (
        (1.0, 0.10, 0.20, 8.8),
        (-1.0, 0.10, 0.20, 13.2),
    ),
)
def test_full_position_disaster_stop_sits_outside_all_component_stops(
    tmp_path: Path,
    signed_qty: float,
    first_stop_loss_pct: float,
    second_stop_loss_pct: float,
    expected_stop: float,
) -> None:
    kernel, clock = _open_position(
        tmp_path,
        signed_qty=signed_qty,
        metadata={"stop_loss_pct": first_stop_loss_pct},
    )
    market = MarketInputRef(
        input_key="book-component-b",
        symbol="BUSDT",
        exchange_ts_ns=1_700_000_000,
        local_receive_ts_ns=1_710_000_000,
        reference_price=10.0,
    )
    sleeve = "long" if signed_qty > 0.0 else "continuous"
    added = kernel.submit_targets(
        batch_id="open-component-b",
        market_inputs=(market,),
        targets=(DesiredTarget(
            decision_key="open-component-b-decision",
            target_key=f"{sleeve}/strategy/component-b/BUSDT",
            sleeve=sleeve,
            strategy_id="strategy",
            component_id="component-b",
            symbol="BUSDT",
            signed_qty=signed_qty,
            reference_price=10.0,
            leverage=10.0,
            reason="entry",
            metadata={"stop_loss_pct": second_stop_loss_pct},
        ),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet-b", 1_705_000_000),
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1),
        },
    )
    command = added.commands[0]
    KernelExecutionDriver(kernel).ingest((
        ExecutionObservation(
            observation_type="ack",
            command_id=command.command_id,
            exchange_ts_ns=1_720_000_000,
            local_receive_ts_ns=1_721_000_000,
            accepted=True,
            venue_order_id="component-b-order",
        ),
        ExecutionObservation(
            observation_type="fill",
            command_id=command.command_id,
            exchange_ts_ns=1_730_000_000,
            local_receive_ts_ns=1_731_000_000,
            venue_order_id="component-b-order",
            execution_id="component-b-fill",
            signed_qty=signed_qty,
            price=11.0,
            fee_usdt=0.01,
        ),
    ))
    manager, client = _manager(kernel, clock)

    plan = manager.sync("BUSDT")

    assert plan is not None
    assert plan.stop_price == expected_stop
    assert plan.stop_source == "fill_anchored_outermost_component_stop"
    assert client.stops[-1]["tpsl_mode"] == "Full"


def test_venue_snapshot_repairs_missing_native_stop_even_when_local_state_is_active(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, client = _manager(kernel, clock)
    plan = manager.sync("BUSDT")
    assert plan is not None
    assert len(client.stops) == 1

    clock.advance_ns(2_000_000_000)
    manager.reconcile_venue_positions([{
        "symbol": "BUSDT",
        "side": "Sell",
        "size": "2",
        "stopLoss": "0",
    }])

    assert len(client.stops) == 2
    assert client.stops[-1]["stop_loss"] == "10.7"
    active = manager.active("BUSDT")
    assert active is not None
    assert active[0] != plan.protection_key
    assert active[1]["metadata"]["activation_revision"] == 2
    assert kernel.state().protections[plan.protection_key]["status"] == "replaced"
    manager.require_recent_healthy(max_age_ns=1)


def test_full_stop_update_accepts_bybit_reused_order_id_as_native_lineage(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, client = _manager(kernel, clock)
    first = manager.sync("BUSDT")
    assert first is not None
    original_order = {
        "symbol": "BUSDT",
        "orderId": "reused-full-stop",
        "orderLinkId": "",
        "orderStatus": "Untriggered",
        "cumExecQty": "0",
        "triggerPrice": "10.7",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }
    assert manager.observe_order(original_order) is True

    # A Full-position repair/update can mutate the existing system order. The
    # API response has no order identity, so the next REST/WS row is the proof
    # that Bybit retained the same orderId for the active stop.
    repaired = manager.sync("BUSDT", force=True)
    assert repaired == first
    assert len(client.stops) == 2
    assert manager.observed_native_order_ids == {}
    active = manager.active("BUSDT")
    assert active is not None
    assert active[1]["metadata"]["native_venue_order_id_lineage"] == [
        "reused-full-stop"
    ]
    assert manager.is_verified_native_order(original_order) is True
    assert manager.is_verified_native_order(
        {**original_order, "triggerPrice": "10.8"}
    ) is False
    assert manager.native_execution_identity_evidence(original_order) == (
        "matched_known_native_order_lineage"
    )

    class ConditionalOrderClient:
        demo = True

        def get_open_orders(self, **_params: object):
            return [original_order]

    ownership = inspect_bybit_demo_order_ownership(
        client=ConditionalOrderClient(),
        state=kernel._state_ref(),
        native_order_verifier=manager.is_verified_native_order,
    )
    assert ownership.unowned_orders == ()

    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )
    consumer.on_execution(
        {"data": [{
            "symbol": "BUSDT",
            "orderId": "reused-full-stop",
            "orderLinkId": "",
            "execId": "reused-full-stop-fill",
            "side": "Buy",
            "execQty": "2",
            "execPrice": "10.8",
            "execFee": "0.02",
            "execTime": "2100",
            "execType": "Trade",
            "createType": "CreateByStopLoss",
            "stopOrderType": "StopLoss",
        }]},
        local_receive_ts_ns=2_100_000_000,
    )

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    execution = state.executions["reused-full-stop-fill"]
    assert execution["metadata"]["external_execution_origin"] == "verified_native_stop"
    assert execution["metadata"]["native_identity"] == (
        "matched_known_native_order_lineage"
    )
    assert next(iter(state.closes.values()))["reason"] == "native_protection_triggered"


def test_unknown_bybit_stop_execution_is_adopted_and_never_reopens(tmp_path: Path) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, client = _manager(kernel, clock)
    plan = manager.sync("BUSDT")
    assert plan is not None
    assert plan.stop_price == 10.7
    assert client.stops[0]["stop_loss"] == "10.7"

    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )
    consumer.on_order({"data": [{
        "symbol": "BUSDT",
        "orderLinkId": "",
        "orderId": "bybit-native-stop",
        "orderStatus": "Triggered",
        "cumExecQty": "0",
        "triggerPrice": "10.7",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }]}, local_receive_ts_ns=2_050_000_000)
    consumer.on_execution(
        {
            "data": [{
                "symbol": "BUSDT",
                "orderLinkId": "",
                "orderId": "bybit-native-stop",
                "execId": "native-stop-fill",
                "side": "Buy",
                "execQty": "2",
                "execPrice": "10.8",
                "execFee": "0.02",
                "execTime": "1700",
                "execType": "Trade",
                "createType": "CreateByStopLoss",
                "stopOrderType": "StopLoss",
            }],
        },
        local_receive_ts_ns=2_100_000_000,
    )

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert state.aggregate_targets["BUSDT"] == 0.0
    assert state.component_targets == {}
    assert any(
        event.event_type == AccountEventType.TARGET.value
        and event.payload.get("target_key") == "continuous/strategy/trade/BUSDT"
        and float(event.payload.get("signed_qty") or 0.0) == 0.0
        for event in read_account_journal(tmp_path)
    )
    assert len(state.closes) == 1
    assert len(state.pnl) == 1
    assert state.protections[plan.protection_key]["status"] == "triggered"
    assert (
        state.executions["native-stop-fill"]["metadata"]["native_identity"]
        == "matched_verified_native_order_event"
    )


def test_native_stop_partial_fill_survives_restart_and_finishes_same_command(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    plan = manager.sync("BUSDT")
    assert plan is not None
    first_consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )
    first_consumer.on_order({"data": [{
        "symbol": "BUSDT",
        "orderLinkId": "",
        "orderId": "native-partial-order",
        "orderStatus": "Triggered",
        "cumExecQty": "0",
        "triggerPrice": "10.7",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }]}, local_receive_ts_ns=2_050_000_000)
    first_consumer.on_execution(
        {"data": [{
            "symbol": "BUSDT",
            "orderId": "native-partial-order",
            "execId": "native-part-1",
            "side": "Buy",
            "execQty": "0.5",
            "execPrice": "10.7",
            "execFee": "0.005",
            "execTime": "1700",
            "createType": "CreateByStopLoss",
            "stopOrderType": "StopLoss",
        }]},
        local_receive_ts_ns=2_100_000_000,
    )
    partial = kernel.state()
    assert partial.positions["BUSDT"].signed_qty == -1.5
    assert partial.aggregate_targets["BUSDT"] == 0.0
    assert partial.protections[plan.protection_key]["status"] == "triggering"
    assert partial.closes == {}
    with pytest.raises(RuntimeError, match="trigger is unresolved"):
        manager.require_recent_healthy(max_age_ns=1_000_000_000)

    restarted = AccountExecutionKernel(
        tmp_path,
        account_id="demo",
        clock=clock,
        id_seed="native",
    )
    manager_after_restart, _ = _manager(restarted, clock)
    second_consumer = BybitAccountExecutionConsumer(
        kernel=restarted,
        native_protection_manager=manager_after_restart,
        clock=clock,
    )
    second_consumer.on_execution(
        {"data": [{
            "symbol": "BUSDT",
            "orderId": "native-partial-order",
            "execId": "native-part-2",
            "side": "Buy",
            "execQty": "1.5",
            "execPrice": "10.8",
            "execFee": "0.015",
            "execTime": "1701",
            "createType": "CreateByStopLoss",
            "stopOrderType": "StopLoss",
        }]},
        local_receive_ts_ns=2_200_000_000,
    )
    final = restarted.state()
    assert final.positions["BUSDT"].signed_qty == 0.0
    external_orders = [
        order for order in final.orders.values() if order.venue_order_id == "native-partial-order"
    ]
    assert len(external_orders) == 1
    assert external_orders[0].status == "filled"
    assert len(final.closes) == 1
    assert len(final.pnl) == 1
    assert final.protections[plan.protection_key]["status"] == "triggered"
    assert final.protections[plan.protection_key]["metadata"][
        "completed_via_joined_venue_order_id"
    ] is True


def test_native_terminal_before_execution_is_joined_by_venue_order_id(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    manager.sync("BUSDT")
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )
    consumer.on_order({"data": [{
        "symbol": "BUSDT",
        "orderLinkId": "",
        "orderId": "native-race-order",
        "orderStatus": "Cancelled",
        "cumExecQty": "0.5",
        "updatedTime": "2200",
        "triggerPrice": "10.7",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }]}, local_receive_ts_ns=2_200_000_000)
    assert "native-race-order" in consumer.pending_native_terminal
    with pytest.raises(RuntimeError, match="execution recovery pending"):
        manager.require_recent_healthy(max_age_ns=1_000_000_000)

    consumer.on_execution({"creationTime": "2201", "data": [{
        "symbol": "BUSDT",
        "orderLinkId": "",
        "orderId": "native-race-order",
        "execId": "native-race-fill",
        "side": "Buy",
        "execQty": "0.5",
        "execPrice": "10.8",
        "execFee": "0.005",
        "execTime": "2199",
        # The earlier verified order event is enough to bind this order id even
        # if a demo execution row omits its stop provenance fields.
    }]}, local_receive_ts_ns=2_201_000_000)

    state = kernel.state()
    adopted = [
        order for order in state.orders.values() if order.venue_order_id == "native-race-order"
    ]
    assert len(adopted) == 1
    assert adopted[0].status == "partially_filled_cancelled"
    assert state.positions["BUSDT"].signed_qty == -1.5
    assert state.aggregate_targets["BUSDT"] == 0.0
    assert state.working_signed_qty("BUSDT") == 0.0
    assert consumer.pending_native_terminal == {}
    assert (
        state.executions["native-race-fill"]["metadata"]["native_identity"]
        == "matched_verified_native_order_event"
    )
    assert manager.active("BUSDT") is None
    assert any(
        protection["status"] == "cancelled_with_residual"
        for protection in state.protections.values()
    )
    with pytest.raises(RuntimeError, match="residual position"):
        manager.require_recent_healthy(max_age_ns=1_000_000_000)


def test_unfilled_native_stop_cancel_immediately_fails_protection_health(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    plan = manager.sync("BUSDT")
    assert plan is not None
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )

    consumer.on_order({"data": [{
        "symbol": "BUSDT",
        "orderLinkId": "",
        "orderId": "cancelled-native-order",
        "orderStatus": "Cancelled",
        "cumExecQty": "0",
        "updatedTime": "2200",
        "triggerPrice": "10.7",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }]}, local_receive_ts_ns=2_200_000_000)

    state = kernel.state()
    assert state.protections[plan.protection_key]["status"] == "cancelled_unfilled"
    assert consumer.pending_native_terminal == {}
    with pytest.raises(RuntimeError, match="cancelled unfilled"):
        manager.require_recent_healthy(max_age_ns=1_000_000_000)


def test_manual_reduction_is_adopted_without_native_stop_label(tmp_path: Path) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    manager.sync("BUSDT")
    observed_executions: list[str] = []
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        fill_observer=observed_executions.append,
        clock=clock,
    )

    consumer.on_execution({"creationTime": "2201", "data": [{
        "symbol": "BUSDT",
        "orderLinkId": "manual-close",
        "orderId": "manual-order",
        "execId": "manual-fill",
        "side": "Buy",
        "execQty": "2",
        "execPrice": "10.5",
        "execFee": "0.01",
        "feeRate": "0.00055",
        "feeCurrency": "USDT",
        "isMaker": False,
        "execType": "Trade",
        "execValue": "21",
        "orderQty": "2",
        "leavesQty": "0",
        "closedSize": "2",
        "orderType": "Market",
        "execTime": "2200",
        "createType": "CreateByUser",
        "stopOrderType": "UNKNOWN",
    }]}, local_receive_ts_ns=2_200_000_000)

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert state.aggregate_targets["BUSDT"] == 0.0
    execution = state.executions["manual-fill"]
    assert execution["metadata"]["external_execution_origin"] == (
        "unattributed_external_reduction"
    )
    assert execution["metadata"]["external_native_protection"] is False
    assert execution["metadata"]["native_identity"] == ""
    assert execution["metadata"]["fee_rate"] == "0.00055"
    assert execution["metadata"]["fee_currency"] == "USDT"
    assert execution["metadata"]["is_maker"] is False
    assert execution["metadata"]["execution_type"] == "Trade"
    assert execution["metadata"]["execution_value"] == "21"
    assert execution["metadata"]["order_qty"] == "2"
    assert execution["metadata"]["leaves_qty"] == "0"
    assert execution["metadata"]["closed_size"] == "2"
    assert execution["metadata"]["order_type"] == "Market"
    assert execution["metadata"]["message_creation_ts_ns"] == 2_201_000_000
    assert observed_executions == ["manual-fill"]
    assert any(
        protection["status"] == "external_reduction_flat"
        and protection["metadata"]["external_native_protection"] is False
        for protection in state.protections.values()
    )
    close = next(iter(state.closes.values()))
    assert close["reason"] == "external_unattributed_reduction"
    manager.require_recent_healthy(max_age_ns=1_000_000_000)


def test_external_reduction_split_across_order_ids_remains_adoptable(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    manager.sync("BUSDT")
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )
    consumer.on_execution({"data": [{
        "symbol": "BUSDT",
        "orderId": "manual-part-1",
        "execId": "manual-exec-1",
        "side": "Buy",
        "execQty": "0.5",
        "execPrice": "10.5",
        "execFee": "0.005",
        "execTime": "2200",
        "execType": "Trade",
        "createType": "CreateByUser",
    }]}, local_receive_ts_ns=2_200_000_000)
    consumer.on_order({"data": [{
        "symbol": "BUSDT",
        "orderId": "manual-part-1",
        "orderStatus": "Cancelled",
        "cumExecQty": "0.5",
        "updatedTime": "2201",
    }]}, local_receive_ts_ns=2_201_000_000)

    consumer.on_execution({"data": [{
        "symbol": "BUSDT",
        "orderId": "manual-part-2",
        "execId": "manual-exec-2",
        "side": "Buy",
        "execQty": "1.5",
        "execPrice": "10.6",
        "execFee": "0.015",
        "execTime": "2202",
        "execType": "Trade",
        "createType": "CreateByUser",
    }]}, local_receive_ts_ns=2_202_000_000)

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert state.aggregate_targets["BUSDT"] == 0.0
    assert {"manual-exec-1", "manual-exec-2"}.issubset(state.executions)
    external_orders = [
        order
        for order in state.orders.values()
        if order.batch_id.startswith("external-reduction/")
    ]
    assert len(external_orders) == 2
    assert all(
        execution["metadata"]["external_native_protection"] is False
        for execution_id, execution in state.executions.items()
        if execution_id.startswith("manual-exec-")
    )


def test_funding_execution_is_not_treated_as_unaccounted_position_mutation(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    manager.sync("BUSDT")
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )

    consumer.on_execution({"data": [{
        "symbol": "BUSDT",
        "execId": "funding-1",
        "execType": "Funding",
        "execTime": "2200",
    }]}, local_receive_ts_ns=2_200_000_000)

    assert kernel.state().positions["BUSDT"].signed_qty == -2.0
    assert "funding-1" not in kernel.state().executions
    assert manager.last_error == ""
    manager.require_recent_healthy(max_age_ns=1_000_000_000)
