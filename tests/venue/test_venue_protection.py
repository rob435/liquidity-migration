from __future__ import annotations

import threading
from pathlib import Path

import pytest

from liquidity_migration.venue.account_execution_stream import BybitAccountExecutionConsumer
from liquidity_migration.account.account_kernel import (
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
    TargetBatchResult,
    read_account_journal,
)
from liquidity_migration.venue.account_service_bybit import inspect_bybit_order_ownership
from liquidity_migration.marketdata.bybit_errors import BybitRequestRejected
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.execution_adapters import ExecutionObservation, KernelExecutionDriver
from liquidity_migration.venue.venue_protection import (
    BybitNativeProtectionManager,
    NativeProtectionBreachError,
)


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
    entry_attached: bool = False,
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
        targets=(
            DesiredTarget(
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
            ),
        ),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 1_500_000_000),
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1),
        },
        native_protection_policy=(NativeDisasterProtectionPolicy(0.2) if entry_attached else None),
    )
    command = result.commands[0]
    KernelExecutionDriver(kernel).ingest(
        (
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
        )
    )
    return kernel, clock


def _manager(kernel: AccountExecutionKernel, clock: VirtualClock) -> tuple[BybitNativeProtectionManager, _DemoClient]:
    client = _DemoClient()
    manager = BybitNativeProtectionManager(
        kernel=kernel,
        client=client,
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1, environment="demo"),
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
        market_inputs=(
            MarketInputRef(
                input_key=f"book-{batch_id}",
                symbol="BUSDT",
                exchange_ts_ns=1_800_000_000,
                local_receive_ts_ns=1_810_000_000,
                reference_price=10.0,
            ),
        ),
        targets=(
            DesiredTarget(
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
            ),
        ),
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
    KernelExecutionDriver(kernel).ingest(
        (
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
        )
    )
    partial = manager.sync("BUSDT")
    assert partial is not None
    assert partial.protection_key == installed.protection_key
    assert partial.signed_qty == 1.5
    assert len(client.stops) == 1

    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="fill",
                command_id=command.command_id,
                exchange_ts_ns=1_840_000_000,
                local_receive_ts_ns=1_841_000_000,
                venue_order_id="close-order",
                execution_id="close-final",
                signed_qty=-1.5,
                price=10.0,
                fee_usdt=0.01,
            ),
        )
    )
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
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=command.command_id,
                exchange_ts_ns=1_820_000_000,
                local_receive_ts_ns=1_821_000_000,
                accepted=False,
                rejection_key="venue-rejected-close",
            ),
        )
    )

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
    assert client.stops == [
        {
            "symbol": "BUSDT",
            "tpsl_mode": "Full",
            "position_idx": 0,
            "stop_loss": "9.6",
            "take_profit": "0",
            "sl_trigger_by": "MarkPrice",
            "tp_trigger_by": None,
        }
    ]
    manager.require_recent_healthy(max_age_ns=1_000_000_000)

    clock.advance_ns(1_000_000_001)
    with pytest.raises(RuntimeError, match="stale"):
        manager.require_recent_healthy(max_age_ns=1_000_000_000)
    manager.reconcile_venue_positions(
        [
            {
                "symbol": "BUSDT",
                "side": "Buy",
                "size": "2",
                "stopLoss": "9.6",
            }
        ]
    )
    manager.require_recent_healthy(max_age_ns=1_000_000_000)
    assert len(client.stops) == 1


class _PositionReadbackClient(_DemoClient):
    """Demo client that can answer the B5 post-create position read-back."""

    def __init__(
        self,
        rows: list[dict[str, object]] | None = None,
        *,
        stop_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.rows = rows if rows is not None else []
        self.stop_error = stop_error
        self.position_queries = 0

    def get_positions(self, **_params: object) -> list[dict[str, object]]:
        self.position_queries += 1
        return list(self.rows)

    def set_trading_stop(self, **params: object) -> dict[str, object]:
        if self.stop_error is not None:
            raise self.stop_error
        return super().set_trading_stop(**params)


def _readback_manager(
    kernel: AccountExecutionKernel,
    clock: VirtualClock,
    client: _PositionReadbackClient,
) -> BybitNativeProtectionManager:
    return BybitNativeProtectionManager(
        kernel=kernel,
        client=client,
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1, environment="demo"),
        },
        fallback_stop_fraction=0.07,
        clock=clock,
    )


def test_entry_attached_stop_verified_when_the_venue_applied_it(tmp_path: Path) -> None:
    """B5: the happy path proves arming from venue truth, and mutates nothing."""

    kernel, clock = _open_position(tmp_path, signed_qty=2.0, entry_attached=True)
    client = _PositionReadbackClient(
        [{"symbol": "BUSDT", "side": "Buy", "size": "2", "stopLoss": "8", "markPrice": "10"}]
    )
    manager = _readback_manager(kernel, clock, client)

    verdict = manager.verify_entry_attached_stop(
        symbol="BUSDT", expected_stop_price=8.0, command_id="cmd-1"
    )

    assert verdict == "armed"
    assert client.stops == []


def test_entry_attached_stop_dropped_by_the_venue_is_installed_immediately(
    tmp_path: Path,
) -> None:
    """B5: the venue accepted the order but dropped the stop; install it now."""

    kernel, clock = _open_position(tmp_path, signed_qty=2.0, entry_attached=True)
    client = _PositionReadbackClient(
        [{"symbol": "BUSDT", "side": "Buy", "size": "2", "markPrice": "10"}]
    )
    manager = _readback_manager(kernel, clock, client)

    verdict = manager.verify_entry_attached_stop(
        symbol="BUSDT", expected_stop_price=8.0, command_id="cmd-1"
    )

    assert verdict == "repaired"
    assert client.stops == [
        {
            "symbol": "BUSDT",
            "tpsl_mode": "Full",
            "position_idx": 0,
            "stop_loss": "8",
            "take_profit": "0",
            "sl_trigger_by": "MarkPrice",
            "tp_trigger_by": None,
        }
    ]


def test_unrepairable_entry_stop_blocks_health_and_flattens_on_the_next_pass(
    tmp_path: Path,
) -> None:
    """B5: absent stop that cannot be installed becomes a flatten, not a shrug."""

    kernel, clock = _open_position(tmp_path, signed_qty=2.0, entry_attached=True)
    rejection = BybitRequestRejected(
        "Bybit set_trading_stop failed: 10001 stoploss[8] must be less than base_price[7.5]"
    )
    client = _PositionReadbackClient(
        [{"symbol": "BUSDT", "side": "Buy", "size": "2", "markPrice": "7.5"}],
        stop_error=rejection,
    )
    manager = _readback_manager(kernel, clock, client)

    verdict = manager.verify_entry_attached_stop(
        symbol="BUSDT", expected_stop_price=8.0, command_id="cmd-1"
    )
    assert verdict == "breached"

    # New exposure is refused from this instant, before any reconciliation tick.
    with pytest.raises(RuntimeError, match="did not apply an entry-attached stop"):
        manager.require_recent_healthy(max_age_ns=10_000_000_000)

    # The next reconciliation converts the latch into the durable breach that
    # the protection engine turns into a software flat request.
    with pytest.raises(Exception):
        manager.reconcile_venue_positions(
            [{"symbol": "BUSDT", "side": "Buy", "size": "2", "markPrice": "7.5"}]
        )
    assert [breach.plan.symbol for breach in manager.breaches()] == ["BUSDT"]


def test_a_stop_that_lands_late_clears_the_latch_instead_of_flattening(
    tmp_path: Path,
) -> None:
    """A late-but-correct stop is not a reason to close a live position."""

    kernel, clock = _open_position(tmp_path, signed_qty=2.0, entry_attached=True)
    client = _PositionReadbackClient(
        [{"symbol": "BUSDT", "side": "Buy", "size": "2", "markPrice": "10"}],
        stop_error=BybitRequestRejected("Bybit set_trading_stop failed: transient"),
    )
    manager = _readback_manager(kernel, clock, client)
    # 9.3 is this manager's own plan for the position, so the reconciliation
    # walk below agrees with the entry-attached price rather than repairing it.
    assert (
        manager.verify_entry_attached_stop(
            symbol="BUSDT", expected_stop_price=9.3, command_id="cmd-1"
        )
        == "breached"
    )

    manager.reconcile_venue_positions(
        [{"symbol": "BUSDT", "side": "Buy", "size": "2", "stopLoss": "9.3", "markPrice": "10"}]
    )

    assert manager.breaches() == ()
    manager.require_recent_healthy(max_age_ns=10_000_000_000)


def test_unreadable_position_truth_after_a_create_fails_closed(tmp_path: Path) -> None:
    """B5: no position visible is not evidence of an armed one."""

    kernel, clock = _open_position(tmp_path, signed_qty=2.0, entry_attached=True)
    client = _PositionReadbackClient([])
    manager = _readback_manager(kernel, clock, client)

    verdict = manager.verify_entry_attached_stop(
        symbol="BUSDT", expected_stop_price=8.0, command_id="cmd-1", attempts=2
    )

    assert verdict == "position_not_visible"
    assert client.position_queries == 2
    assert client.stops == []
    with pytest.raises(RuntimeError, match="unverified"):
        manager.require_recent_healthy(max_age_ns=10_000_000_000)


def test_skipped_symbol_is_left_stale_rather_than_marked_verified(tmp_path: Path) -> None:
    """B6: a skipped symbol must not be repaired, and must not look fresh."""

    kernel, clock = _open_position(
        tmp_path,
        signed_qty=2.0,
        fill_price=11.0,
        metadata={"stop_loss_pct": 0.125},
    )
    manager, client = _manager(kernel, clock)
    manager.sync("BUSDT")
    manager.require_recent_healthy(max_age_ns=1_000_000_000)
    clock.advance_ns(1_000_000_001)

    row = {"symbol": "BUSDT", "side": "Buy", "size": "2", "stopLoss": "9.6"}
    manager.reconcile_venue_positions([row], skip_symbols=frozenset({"BUSDT"}))

    # No repair attempted, and the freshness clock was NOT advanced: the symbol
    # still fails its own staleness check rather than borrowing a proof it did
    # not earn.
    assert len(client.stops) == 1
    with pytest.raises(RuntimeError, match="stale"):
        manager.require_recent_healthy(max_age_ns=1_000_000_000)

    # The same pass without the skip proves it and clears the staleness.
    manager.reconcile_venue_positions([row])
    manager.require_recent_healthy(max_age_ns=1_000_000_000)


def test_entry_attached_stop_is_owned_before_exact_post_fill_reanchor(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(
        tmp_path,
        signed_qty=2.0,
        entry_attached=True,
    )
    manager, _client = _manager(kernel, clock)
    attached_row = {
        "symbol": "BUSDT",
        "orderId": "entry-attached-stop-1",
        "orderLinkId": "",
        "orderStatus": "Untriggered",
        "cumExecQty": "0",
        "triggerPrice": "8",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }

    assert manager.is_verified_native_order(attached_row)
    assert manager.observe_order(attached_row)
    active = manager.active("BUSDT")
    assert active is not None
    assert active[1]["stop_price"] == 8.0
    assert active[1]["metadata"]["entry_attached_provisional"] is True
    assert active[1]["metadata"]["entry_command_id"] in kernel.state().orders
    manager.require_recent_healthy(max_age_ns=1_000_000_000)

    assert not manager.is_verified_native_order(
        {
            **attached_row,
            "orderId": "foreign-stop",
            "triggerPrice": "7.9",
        }
    )
    assert not manager.is_verified_native_order(
        {
            **attached_row,
            "orderId": "foreign-same-price-stop",
            "orderLinkId": "manual-foreign-link",
        }
    )


def test_newer_entry_attached_stop_replaces_stale_local_native_binding(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(
        tmp_path,
        signed_qty=2.0,
        metadata={"stop_loss_pct": 0.2},
        entry_attached=True,
    )
    first_command = next(iter(kernel.state().orders.values()))
    kernel.record_protection(
        protection_key="native-old-exact",
        symbol="BUSDT",
        status="active",
        stop_price=8.0,
        take_profit_price=None,
        exchange_ts_ns=1_700_000_000,
        local_receive_ts_ns=1_710_000_000,
        command_id=first_command.command_id,
        metadata={
            "native_exchange": True,
            "protection_plan_key": "native-old-exact",
            "activation_revision": 1,
            "symbol": "BUSDT",
            "signed_qty": 2.0,
            "trigger_by": "MarkPrice",
            "venue_order_id": "old-native-order",
        },
    )
    market = MarketInputRef(
        input_key="scale-book",
        symbol="BUSDT",
        exchange_ts_ns=1_800_000_000,
        local_receive_ts_ns=1_900_000_000,
        reference_price=10.0,
    )
    scaled = kernel.submit_targets(
        batch_id="entry-attached-scale",
        market_inputs=(market,),
        targets=(
            DesiredTarget(
                decision_key="entry-attached-scale",
                target_key="long/strategy/trade/BUSDT",
                sleeve="long",
                strategy_id="strategy",
                component_id="trade",
                symbol="BUSDT",
                signed_qty=4.0,
                reference_price=10.0,
                leverage=10.0,
                reason="scale",
                metadata={"stop_loss_pct": 0.3},
            ),
        ),
        risk_snapshot=AccountRiskSnapshot(
            10_000.0,
            9_000.0,
            "wallet-scale",
            1_950_000_000,
        ),
        risk_policy=AccountRiskPolicy(
            1_000.0,
            1_000.0,
            1_000.0,
            1_000.0,
            10.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT",
                0.1,
                0.1,
                1.0,
                tick_size=0.1,
            ),
        },
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )
    assert scaled.accepted
    newer_command = scaled.commands[0]
    assert newer_command.entry_stop_price == 7.0
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=newer_command.command_id,
                exchange_ts_ns=1_960_000_000,
                local_receive_ts_ns=1_970_000_000,
                accepted=True,
                venue_order_id="scale-entry-order",
            ),
            ExecutionObservation(
                observation_type="fill",
                command_id=newer_command.command_id,
                exchange_ts_ns=1_980_000_000,
                local_receive_ts_ns=1_990_000_000,
                venue_order_id="scale-entry-order",
                execution_id="scale-entry-fill",
                signed_qty=2.0,
                price=10.0,
                fee_usdt=0.01,
            ),
        )
    )
    state = kernel.state()
    assert state.orders[newer_command.command_id].command_sequence > (
        state.orders[first_command.command_id].command_sequence
    )

    manager, _client = _manager(kernel, clock)
    newer_stop_row = {
        "symbol": "BUSDT",
        "orderId": "new-entry-attached-stop",
        "orderLinkId": newer_command.command_id,
        "orderStatus": "Untriggered",
        "cumExecQty": "0",
        "triggerPrice": "7",
        "side": "Sell",
        "positionIdx": 0,
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
    )
    consumer.on_order(
        {"creationTime": "2000", "data": [newer_stop_row]},
        local_receive_ts_ns=2_000_000_000,
    )
    active = manager.active("BUSDT")
    assert active is not None
    assert active[1]["stop_price"] == 7.0
    assert active[1]["metadata"]["entry_command_id"] == (
        newer_command.command_id
    )
    assert active[1]["metadata"]["venue_order_id"] == (
        "new-entry-attached-stop"
    )
    assert kernel.state().protections["native-old-exact"]["status"] == "replaced"
    assert kernel.state().orders[newer_command.command_id].status == "filled"


@pytest.mark.parametrize("child_uses_parent_link", [False, True])
def test_entry_fill_is_committed_before_same_message_attached_stop_trigger(
    tmp_path: Path,
    child_uses_parent_link: bool,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(
        tmp_path,
        account_id="demo",
        clock=clock,
        id_seed="entry-attached-race",
    )
    market = MarketInputRef(
        input_key="entry-book",
        symbol="BUSDT",
        exchange_ts_ns=1_000_000_000,
        local_receive_ts_ns=1_100_000_000,
        reference_price=10.0,
    )
    result = kernel.submit_targets(
        batch_id="entry-attached-race",
        market_inputs=(market,),
        targets=(
            DesiredTarget(
                decision_key="entry-race-decision",
                target_key="long/strategy/race/BUSDT",
                sleeve="long",
                strategy_id="strategy",
                component_id="race",
                symbol="BUSDT",
                signed_qty=2.0,
                reference_price=10.0,
                leverage=10.0,
                reason="entry",
            ),
        ),
        risk_snapshot=AccountRiskSnapshot(
            10_000.0,
            9_000.0,
            "wallet",
            1_500_000_000,
        ),
        risk_policy=AccountRiskPolicy(
            1_000.0,
            1_000.0,
            1_000.0,
            1_000.0,
            10.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT",
                0.1,
                0.1,
                1.0,
                tick_size=0.1,
            ),
        },
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )
    command = result.commands[0]
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=command.command_id,
                exchange_ts_ns=1_500_000_000,
                local_receive_ts_ns=1_510_000_000,
                accepted=True,
                venue_order_id="entry-order",
            ),
        )
    )
    manager, client = _manager(kernel, clock)
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
    )
    consumer.on_execution(
        {
            "creationTime": "1700",
            # Deliberately stop-first: the consumer must establish the entry
            # position before attempting to adopt its reduction.
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderId": "entry-stop-order",
                    "orderLinkId": command.command_id if child_uses_parent_link else "",
                    "execId": "entry-stop-fill",
                    "execQty": "2",
                    "execPrice": "8",
                    "execFee": "0.01",
                    "execTime": "1690",
                    "side": "Sell",
                    "triggerPrice": "8",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                },
                {
                    "symbol": "BUSDT",
                    "orderId": "entry-order",
                    "orderLinkId": command.command_id,
                    "execId": "entry-fill",
                    "execQty": "2",
                    "execPrice": "10",
                    "execFee": "0.01",
                    "execTime": "1680",
                    "side": "Buy",
                    "createType": "CreateByUser",
                    "execType": "Trade",
                },
            ],
        },
        local_receive_ts_ns=1_700_000_000,
    )

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert state.aggregate_targets["BUSDT"] == 0.0
    assert state.executions["entry-stop-fill"]["metadata"]["external_execution_origin"] == "verified_native_stop"
    assert any(
        protection["status"] == "triggered" and protection["metadata"]["entry_attached_provisional"] is True
        for protection in state.protections.values()
    )
    assert client.stops == []


def test_scale_fill_precedes_parent_linked_attached_stop_despite_old_binding(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(
        tmp_path,
        signed_qty=2.0,
        metadata={"stop_loss_pct": 0.2},
        entry_attached=True,
    )
    first_command = next(iter(kernel.state().orders.values()))
    kernel.record_protection(
        protection_key="old-scale-native",
        symbol="BUSDT",
        status="active",
        stop_price=8.0,
        take_profit_price=None,
        exchange_ts_ns=1_700_000_000,
        local_receive_ts_ns=1_710_000_000,
        command_id=first_command.command_id,
        metadata={
            "native_exchange": True,
            "protection_plan_key": "old-scale-native",
            "activation_revision": 1,
            "symbol": "BUSDT",
            "signed_qty": 2.0,
            "trigger_by": "MarkPrice",
            "venue_order_id": "old-scale-stop-order",
        },
    )
    market = MarketInputRef(
        input_key="scale-race-book",
        symbol="BUSDT",
        exchange_ts_ns=1_800_000_000,
        local_receive_ts_ns=1_900_000_000,
        reference_price=10.0,
    )
    scaled = kernel.submit_targets(
        batch_id="scale-attached-race",
        market_inputs=(market,),
        targets=(
            DesiredTarget(
                decision_key="scale-attached-race",
                target_key="long/strategy/trade/BUSDT",
                sleeve="long",
                strategy_id="strategy",
                component_id="trade",
                symbol="BUSDT",
                signed_qty=4.0,
                reference_price=10.0,
                leverage=10.0,
                reason="scale",
                metadata={"stop_loss_pct": 0.3},
            ),
        ),
        risk_snapshot=AccountRiskSnapshot(
            10_000.0,
            9_000.0,
            "wallet-scale-race",
            1_950_000_000,
        ),
        risk_policy=AccountRiskPolicy(
            1_000.0,
            1_000.0,
            1_000.0,
            1_000.0,
            10.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT",
                0.1,
                0.1,
                1.0,
                tick_size=0.1,
            ),
        },
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )
    command = scaled.commands[0]
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=command.command_id,
                exchange_ts_ns=1_960_000_000,
                local_receive_ts_ns=1_970_000_000,
                accepted=True,
                venue_order_id="scale-race-entry-order",
            ),
        )
    )
    manager, client = _manager(kernel, clock)
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
    )
    consumer.on_execution(
        {
            "creationTime": "2000",
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderId": "scale-race-stop-order",
                    "orderLinkId": command.command_id,
                    "execId": "scale-race-stop-fill",
                    "execQty": "4",
                    "execPrice": "7",
                    "execFee": "0.01",
                    "execTime": "1990",
                    "side": "Sell",
                    "triggerPrice": "7",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                },
                {
                    "symbol": "BUSDT",
                    "orderId": "scale-race-entry-order",
                    "orderLinkId": command.command_id,
                    "execId": "scale-race-entry-fill",
                    "execQty": "2",
                    "execPrice": "10",
                    "execFee": "0.01",
                    "execTime": "1980",
                    "side": "Buy",
                    "createType": "CreateByUser",
                    "execType": "Trade",
                },
            ],
        },
        local_receive_ts_ns=2_000_000_000,
    )

    state = kernel.state()
    assert state.orders[command.command_id].filled_signed_qty == 2.0
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert state.executions["scale-race-stop-fill"]["metadata"]["external_execution_origin"] == (
        "verified_native_stop"
    )
    assert state.protections["old-scale-native"]["status"] == "replaced"
    assert client.stops == []


def test_concurrent_sync_reconcile_and_order_binding_install_one_activation(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    client = _BlockingDemoClient()
    manager = BybitNativeProtectionManager(
        kernel=kernel,
        client=client,
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1, environment="demo"),
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
        manager.reconcile_venue_positions(
            [
                {
                    "symbol": "BUSDT",
                    "side": "Sell",
                    "size": "2",
                    "stopLoss": "10.7",
                }
            ]
        )

    def bind_order() -> None:
        start_contenders.wait(timeout=5.0)
        binding_results.append(
            manager.observe_order(
                {
                    "symbol": "BUSDT",
                    "orderLinkId": "",
                    "orderId": "native-order-1",
                    "orderStatus": "Triggered",
                    "cumExecQty": "0",
                    "triggerPrice": "10.7",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                }
            )
        )

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
    assert (
        manager.native_execution_identity_evidence(
            {
                "symbol": "BUSDT",
                "orderId": "native-order-1",
            }
        )
        == "matched_verified_native_order_event"
    )


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
        targets=(
            DesiredTarget(
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
            ),
        ),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet-b", 1_705_000_000),
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1),
        },
    )
    command = added.commands[0]
    KernelExecutionDriver(kernel).ingest(
        (
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
        )
    )
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
    manager.reconcile_venue_positions(
        [
            {
                "symbol": "BUSDT",
                "side": "Sell",
                "size": "2",
                "stopLoss": "0",
                "markPrice": "10.0",
            }
        ]
    )

    assert len(client.stops) == 2
    assert client.stops[-1]["stop_loss"] == "10.7"
    active = manager.active("BUSDT")
    assert active is not None
    assert active[0] != plan.protection_key
    assert active[1]["metadata"]["activation_revision"] == 2
    assert kernel.state().protections[plan.protection_key]["status"] == "replaced"
    manager.require_recent_healthy(max_age_ns=1)


def test_authenticated_matching_stop_wins_over_crossed_stale_journal_mark(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, client = _manager(kernel, clock)
    plan = manager.sync("BUSDT")
    assert plan is not None
    # Reproduce the incident's frozen latest_market_inputs after new intents
    # were blocked. This local value is not authenticated current venue truth.
    kernel._state_ref().latest_market_inputs["BUSDT"]["reference_price"] = 11.5
    clock.advance_ns(1_000_000_000)

    manager.reconcile_venue_positions(
        [
            {
                "symbol": "BUSDT",
                "side": "Sell",
                "size": "2",
                "stopLoss": "10.7",
                "markPrice": "11.5",
            }
        ]
    )

    assert len(client.stops) == 1
    assert manager.breaches() == ()
    manager.require_recent_healthy(max_age_ns=1)


def test_missing_crossed_stop_becomes_typed_breach_without_mutation_storm(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, client = _manager(kernel, clock)
    plan = manager.sync("BUSDT")
    assert plan is not None

    crossed = {
        "symbol": "BUSDT",
        "side": "Sell",
        "size": "2",
        "stopLoss": "0",
        "markPrice": "11.2",
    }
    with pytest.raises(RuntimeError, match="absent and already crossed"):
        manager.reconcile_venue_positions([crossed])
    # Once breached, a later price recovery cannot silently re-arm the stop
    # and erase the required flat transition.
    recovered_price = {**crossed, "markPrice": "10.0"}
    with pytest.raises(RuntimeError, match="absent and already crossed"):
        manager.reconcile_venue_positions([recovered_price])
    with pytest.raises(RuntimeError, match="absent and already crossed"):
        manager.reconcile_venue_positions([crossed])

    assert len(client.stops) == 1
    assert manager.active("BUSDT") is None
    assert len(manager.breaches()) == 1
    assert manager.breaches()[0].observed_mark == 11.2
    breached_rows = [
        event
        for event in read_account_journal(tmp_path)
        if event.event_type == AccountEventType.PROTECTION.value
        and event.payload.get("status") == "breached_unprotected"
    ]
    assert len(breached_rows) == 1

    restarted = AccountExecutionKernel(
        tmp_path,
        account_id="demo",
        clock=clock,
        id_seed="native",
    )
    after_restart, restarted_client = _manager(restarted, clock)
    assert after_restart.breaches()[0].observed_ts_ns == manager.breaches()[0].observed_ts_ns
    with pytest.raises(RuntimeError, match="absent and already crossed"):
        after_restart.reconcile_venue_positions([crossed])
    assert restarted_client.stops == []
    with pytest.raises(RuntimeError, match="absent and already crossed"):
        after_restart.reconcile_venue_positions([recovered_price])
    assert restarted_client.stops == []
    after_restart.reconcile_venue_positions([{**recovered_price, "stopLoss": "10.7"}])
    assert after_restart.breaches() == ()
    assert restarted_client.stops == []

    restarted_again = AccountExecutionKernel(
        tmp_path,
        account_id="demo",
        clock=clock,
        id_seed="native",
    )
    recovered_manager, _recovered_client = _manager(restarted_again, clock)
    assert recovered_manager.breaches() == ()
    assert recovered_manager.active("BUSDT") is not None
    assert (
        len(
            [
                event
                for event in read_account_journal(tmp_path)
                if event.event_type == AccountEventType.PROTECTION.value
                and event.payload.get("status") == "breached_unprotected"
            ]
        )
        == 1
    )


def test_crossed_stop_breach_requires_authenticated_flat_before_clear(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=2.0)
    manager, _client = _manager(kernel, clock)
    manager.sync("BUSDT")
    crossed = {
        "symbol": "BUSDT",
        "side": "Buy",
        "size": "2",
        "stopLoss": "0",
        "markPrice": "8.0",
    }
    with pytest.raises(RuntimeError, match="absent and already crossed"):
        manager.reconcile_venue_positions([crossed])

    close = _replace_long_target(
        kernel,
        batch_id="locally-flat-after-breach",
        signed_qty=0.0,
    )
    command = close.commands[0]
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=command.command_id,
                exchange_ts_ns=2_100_000_000,
                local_receive_ts_ns=2_110_000_000,
                accepted=True,
                venue_order_id="safety-flat-order",
            ),
            ExecutionObservation(
                observation_type="fill",
                command_id=command.command_id,
                exchange_ts_ns=2_120_000_000,
                local_receive_ts_ns=2_125_000_000,
                venue_order_id="safety-flat-order",
                execution_id="safety-flat-fill",
                signed_qty=-2.0,
                price=8.0,
                fee_usdt=0.01,
            ),
        )
    )
    assert kernel.state().positions["BUSDT"].signed_qty == 0.0

    with pytest.raises(NativeProtectionBreachError):
        manager.sync("BUSDT")
    assert len(manager.breaches()) == 1

    # An empty row set is meaningful here because this API receives the full,
    # successfully fetched Bybit position snapshot.
    manager.reconcile_venue_positions([])
    assert manager.breaches() == ()
    recovered = [
        event
        for event in read_account_journal(tmp_path)
        if event.event_type == AccountEventType.PROTECTION.value
        and event.payload.get("status") == "position_flat_recovered"
    ]
    assert len(recovered) == 1
    assert recovered[0].payload["metadata"]["breach_recovery_evidence_source"] == (
        "bybit_authenticated_position_snapshot"
    )


def test_exact_bybit_integer_price_rejection_is_normalized_to_typed_breach(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)

    class RejectingClient(_DemoClient):
        def set_trading_stop(self, **params: object) -> dict[str, object]:
            self.stops.append(params)
            raise BybitRequestRejected(
                "Bybit set_trading_stop failed: StopLoss:1070000000 set for "
                "Sell position should greater base_price:1120000000??MarkPrice "
                "(ErrCode: 10001)"
            )

    client = RejectingClient()
    manager = BybitNativeProtectionManager(
        kernel=kernel,
        client=client,
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1, environment="demo")},
        fallback_stop_fraction=0.07,
        clock=clock,
    )

    with pytest.raises(NativeProtectionBreachError, match="base price 11.2"):
        manager.sync("BUSDT")
    assert manager.breaches()[0].observed_mark == pytest.approx(11.2)
    with pytest.raises(NativeProtectionBreachError):
        manager.sync("BUSDT")
    assert len(client.stops) == 1


def test_crossed_rejection_shape_that_does_not_cross_position_is_not_authority_to_flat(
    tmp_path: Path,
) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)

    class ContradictoryClient(_DemoClient):
        def set_trading_stop(self, **params: object) -> dict[str, object]:
            self.stops.append(params)
            raise BybitRequestRejected(
                "Bybit set_trading_stop failed: StopLoss:1070000000 set for "
                "Sell position should greater base_price:1000000000??MarkPrice "
                "(ErrCode: 10001)"
            )

    client = ContradictoryClient()
    manager = BybitNativeProtectionManager(
        kernel=kernel,
        client=client,
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1, environment="demo")},
        fallback_stop_fraction=0.07,
        clock=clock,
    )

    with pytest.raises(BybitRequestRejected):
        manager.sync("BUSDT")
    assert manager.breaches() == ()


def test_sync_symbols_attempts_every_sibling_before_raising(tmp_path: Path) -> None:
    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    attempted: list[str] = []

    def sync(symbol: str, **_kwargs: object) -> None:
        attempted.append(symbol)
        if symbol == "AUSDT":
            raise RuntimeError("first symbol failed")

    manager.sync = sync  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="AUSDT.*first symbol failed"):
        manager.sync_symbols(["CUSDT", "AUSDT", "BUSDT"])
    assert attempted == ["AUSDT", "BUSDT", "CUSDT"]


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
    assert active[1]["metadata"]["native_venue_order_id_lineage"] == ["reused-full-stop"]
    assert manager.is_verified_native_order(original_order) is True
    assert manager.is_verified_native_order({**original_order, "triggerPrice": "10.8"}) is False
    assert manager.native_execution_identity_evidence(original_order) == ("matched_known_native_order_lineage")

    class ConditionalOrderClient:
        demo = True
        realm = "demo"

        def get_open_orders(self, **_params: object):
            return [original_order]

    ownership = inspect_bybit_order_ownership(
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
        {
            "data": [
                {
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
                }
            ]
        },
        local_receive_ts_ns=2_100_000_000,
    )

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    execution = state.executions["reused-full-stop-fill"]
    assert execution["metadata"]["external_execution_origin"] == "verified_native_stop"
    assert execution["metadata"]["native_identity"] == ("matched_known_native_order_lineage")
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
    consumer.on_order(
        {
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderLinkId": "",
                    "orderId": "bybit-native-stop",
                    "orderStatus": "Triggered",
                    "cumExecQty": "0",
                    "triggerPrice": "10.7",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                }
            ]
        },
        local_receive_ts_ns=2_050_000_000,
    )
    consumer.on_execution(
        {
            "data": [
                {
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
                }
            ],
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
    assert state.executions["native-stop-fill"]["metadata"]["native_identity"] == "matched_verified_native_order_event"


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
    first_consumer.on_order(
        {
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderLinkId": "",
                    "orderId": "native-partial-order",
                    "orderStatus": "Triggered",
                    "cumExecQty": "0",
                    "triggerPrice": "10.7",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                }
            ]
        },
        local_receive_ts_ns=2_050_000_000,
    )
    first_consumer.on_execution(
        {
            "data": [
                {
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
                }
            ]
        },
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
        {
            "data": [
                {
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
                }
            ]
        },
        local_receive_ts_ns=2_200_000_000,
    )
    final = restarted.state()
    assert final.positions["BUSDT"].signed_qty == 0.0
    external_orders = [order for order in final.orders.values() if order.venue_order_id == "native-partial-order"]
    assert len(external_orders) == 1
    assert external_orders[0].status == "filled"
    assert len(final.closes) == 1
    assert len(final.pnl) == 1
    assert final.protections[plan.protection_key]["status"] == "triggered"
    assert final.protections[plan.protection_key]["metadata"]["completed_via_joined_venue_order_id"] is True


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
    consumer.on_order(
        {
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderLinkId": "",
                    "orderId": "native-race-order",
                    "orderStatus": "Cancelled",
                    "cumExecQty": "0.5",
                    "updatedTime": "2200",
                    "triggerPrice": "10.7",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                }
            ]
        },
        local_receive_ts_ns=2_200_000_000,
    )
    assert "native-race-order" in consumer.pending_native_terminal
    with pytest.raises(RuntimeError, match="execution recovery pending"):
        manager.require_recent_healthy(max_age_ns=1_000_000_000)

    consumer.on_execution(
        {
            "creationTime": "2201",
            "data": [
                {
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
                }
            ],
        },
        local_receive_ts_ns=2_201_000_000,
    )

    state = kernel.state()
    adopted = [order for order in state.orders.values() if order.venue_order_id == "native-race-order"]
    assert len(adopted) == 1
    assert adopted[0].status == "partially_filled_cancelled"
    assert state.positions["BUSDT"].signed_qty == -1.5
    assert state.aggregate_targets["BUSDT"] == 0.0
    assert state.working_signed_qty("BUSDT") == 0.0
    assert consumer.pending_native_terminal == {}
    assert state.executions["native-race-fill"]["metadata"]["native_identity"] == "matched_verified_native_order_event"
    assert manager.active("BUSDT") is None
    assert any(protection["status"] == "cancelled_with_residual" for protection in state.protections.values())
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

    consumer.on_order(
        {
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderLinkId": "",
                    "orderId": "cancelled-native-order",
                    "orderStatus": "Cancelled",
                    "cumExecQty": "0",
                    "updatedTime": "2200",
                    "triggerPrice": "10.7",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                }
            ]
        },
        local_receive_ts_ns=2_200_000_000,
    )

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

    consumer.on_execution(
        {
            "creationTime": "2201",
            "data": [
                {
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
                }
            ],
        },
        local_receive_ts_ns=2_200_000_000,
    )

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert state.aggregate_targets["BUSDT"] == 0.0
    execution = state.executions["manual-fill"]
    assert execution["metadata"]["external_execution_origin"] == ("unattributed_external_reduction")
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
    consumer.on_execution(
        {
            "data": [
                {
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
                }
            ]
        },
        local_receive_ts_ns=2_200_000_000,
    )
    consumer.on_order(
        {
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderId": "manual-part-1",
                    "orderStatus": "Cancelled",
                    "cumExecQty": "0.5",
                    "updatedTime": "2201",
                }
            ]
        },
        local_receive_ts_ns=2_201_000_000,
    )

    consumer.on_execution(
        {
            "data": [
                {
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
                }
            ]
        },
        local_receive_ts_ns=2_202_000_000,
    )

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert state.aggregate_targets["BUSDT"] == 0.0
    assert {"manual-exec-1", "manual-exec-2"}.issubset(state.executions)
    external_orders = [order for order in state.orders.values() if order.batch_id.startswith("external-reduction/")]
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

    consumer.on_execution(
        {
            "data": [
                {
                    "symbol": "BUSDT",
                    "execId": "funding-1",
                    "execType": "Funding",
                    "execTime": "2200",
                }
            ]
        },
        local_receive_ts_ns=2_200_000_000,
    )

    assert kernel.state().positions["BUSDT"].signed_qty == -2.0
    assert "funding-1" not in kernel.state().executions
    assert manager.last_error == ""
    manager.require_recent_healthy(max_age_ns=1_000_000_000)


def test_triggered_native_stop_row_stays_owned_within_visibility_grace(
    tmp_path: Path,
) -> None:
    """A consumed Full stop lingering in the venue open-order cache stays owned for a
    bounded window after the protection record leaves {active, triggering}, then fails
    closed again.
    """

    from liquidity_migration.venue.venue_protection import (
        NATIVE_TERMINAL_ORDER_VISIBILITY_GRACE_NS,
    )

    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    first = manager.sync("BUSDT")
    assert first is not None
    stop_row = {
        "symbol": "BUSDT",
        "orderId": "consumed-full-stop",
        "orderLinkId": "",
        "orderStatus": "Untriggered",
        "cumExecQty": "0",
        "triggerPrice": "10.7",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }
    assert manager.observe_order(stop_row) is True

    # The incident shape: the owner replaced the Full stop (id moves into
    # lineage, live binding cleared), then the stop triggered and its adopted
    # fill flattened the position.
    repaired = manager.sync("BUSDT", force=True)
    assert repaired == first
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )
    consumer.on_execution(
        {
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderLinkId": "",
                    "orderId": "consumed-full-stop",
                    "execId": "consumed-full-stop-fill",
                    "side": "Buy",
                    "execQty": "2",
                    "execPrice": "10.8",
                    "execFee": "0.02",
                    "execTime": "2100",
                    "execType": "Trade",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                }
            ]
        },
        local_receive_ts_ns=2_100_000_000,
    )
    assert kernel.state().positions["BUSDT"].signed_qty == 0.0
    assert manager.active("BUSDT") is None

    clock.advance_ns(200_000_000)
    lingering = {**stop_row, "orderStatus": "Filled", "cumExecQty": "2"}

    # Within the visibility grace the lingering consumed row is still ours.
    assert manager.is_verified_native_order(lingering) is True

    class _LingeringOrderClient:
        demo = True
        realm = "demo"

        def get_open_orders(self, **_params: object):
            return [lingering]

    ownership = inspect_bybit_order_ownership(
        client=_LingeringOrderClient(),
        state=kernel._state_ref(),
        native_order_verifier=manager.is_verified_native_order,
    )
    assert ownership.unowned_orders == ()

    # The stream path is unchanged: nothing is observed without an active
    # protection, and no stale binding is recorded.
    assert manager.observe_order(lingering) is False
    assert manager.observed_native_order_ids == {}

    # A foreign conditional row stays unowned even inside the grace window.
    foreign = {
        **lingering,
        "orderId": "foreign-conditional",
        "triggerPrice": "9.9",
    }
    assert manager.is_verified_native_order(foreign) is False

    # Past the grace window the same lingering row fails closed again.
    clock.advance_ns(NATIVE_TERMINAL_ORDER_VISIBILITY_GRACE_NS + 1)
    assert manager.is_verified_native_order(lingering) is False
    ownership_after = inspect_bybit_order_ownership(
        client=_LingeringOrderClient(),
        state=kernel._state_ref(),
        native_order_verifier=manager.is_verified_native_order,
    )
    assert len(ownership_after.unowned_orders) == 1


def test_visibility_grace_rejects_same_price_foreign_and_partial_rows(
    tmp_path: Path,
) -> None:
    """Identity evidence must match when it exists, partial-stop provenance is never
    grace-owned, and a restart keeps both properties via the journaled record.
    """

    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    assert manager.sync("BUSDT") is not None
    stop_row = {
        "symbol": "BUSDT",
        "orderId": "consumed-full-stop",
        "orderLinkId": "",
        "orderStatus": "Untriggered",
        "cumExecQty": "0",
        "triggerPrice": "10.7",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }
    assert manager.observe_order(stop_row) is True
    manager.sync("BUSDT", force=True)
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )
    consumer.on_execution(
        {
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderLinkId": "",
                    "orderId": "consumed-full-stop",
                    "execId": "consumed-full-stop-fill",
                    "side": "Buy",
                    "execQty": "2",
                    "execPrice": "10.8",
                    "execFee": "0.02",
                    "execTime": "2100",
                    "execType": "Trade",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                }
            ]
        },
        local_receive_ts_ns=2_100_000_000,
    )
    assert manager.active("BUSDT") is None
    clock.advance_ns(200_000_000)

    lingering = {**stop_row, "orderStatus": "Filled", "cumExecQty": "2"}
    assert manager.is_verified_native_order(lingering) is True

    # A different orderId at the SAME trigger price must not be grace-owned
    # while identity evidence (lineage/observed) exists.
    same_price_foreign = {**lingering, "orderId": "foreign-same-price"}
    assert manager.is_verified_native_order(same_price_foreign) is False

    # Partial-stop provenance can never be the lingering consumed Full stop.
    partial = {
        **lingering,
        "stopOrderType": "PartialStopLoss",
        "createType": "CreateByPartialStopLoss",
    }
    assert manager.is_verified_native_order(partial) is False

    # Restart shape: a fresh manager (empty in-memory observed map) keeps both
    # properties from the journaled record alone.
    manager_restarted, _ = _manager(kernel, clock)
    assert manager_restarted.is_verified_native_order(lingering) is True
    assert manager_restarted.is_verified_native_order(same_price_foreign) is False


def test_visibility_grace_first_install_uses_live_observed_binding(
    tmp_path: Path,
) -> None:
    """First-install trigger (no replacement, record lineage empty): the live in-memory
    observed id rejects same-price foreign rows; after a restart no identity evidence
    exists and the bounded price-fallback residual applies.
    """

    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    assert manager.sync("BUSDT") is not None
    stop_row = {
        "symbol": "BUSDT",
        "orderId": "first-install-stop",
        "orderLinkId": "",
        "orderStatus": "Untriggered",
        "cumExecQty": "0",
        "triggerPrice": "10.7",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }
    assert manager.observe_order(stop_row) is True
    consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    )
    consumer.on_execution(
        {
            "data": [
                {
                    "symbol": "BUSDT",
                    "orderLinkId": "",
                    "orderId": "first-install-stop",
                    "execId": "first-install-stop-fill",
                    "side": "Buy",
                    "execQty": "2",
                    "execPrice": "10.8",
                    "execFee": "0.02",
                    "execTime": "2100",
                    "execType": "Trade",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                }
            ]
        },
        local_receive_ts_ns=2_100_000_000,
    )
    assert manager.active("BUSDT") is None
    clock.advance_ns(200_000_000)

    lingering = {**stop_row, "orderStatus": "Filled", "cumExecQty": "2"}
    assert manager.is_verified_native_order(lingering) is True
    same_price_foreign = {**lingering, "orderId": "foreign-same-price"}
    assert manager.is_verified_native_order(same_price_foreign) is False

    # Restart with an identity-empty record: the price fallback is the only
    # evidence left. This bounded residual acceptance is deliberate and
    # documented; pin it so a future change is a conscious decision.
    manager_restarted, _ = _manager(kernel, clock)
    assert manager_restarted.is_verified_native_order(lingering) is True
    assert manager_restarted.is_verified_native_order(same_price_foreign) is True


def test_visibility_grace_bounds_record_exchange_time(tmp_path: Path) -> None:
    """An owner-downtime recovery writes the terminal record late, so the grace window
    must also bound the record's exchange time and cannot reopen a long-dead venue
    window.
    """

    from liquidity_migration.venue.venue_protection import (
        NATIVE_TERMINAL_ORDER_VISIBILITY_GRACE_NS,
    )

    kernel, clock = _open_position(tmp_path, signed_qty=-2.0)
    manager, _client = _manager(kernel, clock)
    clock.advance_ns(2 * NATIVE_TERMINAL_ORDER_VISIBILITY_GRACE_NS)
    kernel.record_protection(
        protection_key="native-disaster:BUSDT:test",
        symbol="BUSDT",
        status="triggered",
        stop_price=10.7,
        take_profit_price=None,
        exchange_ts_ns=clock.wall_time_ns() - NATIVE_TERMINAL_ORDER_VISIBILITY_GRACE_NS - 1,
        local_receive_ts_ns=clock.wall_time_ns(),
        metadata={
            "native_exchange": True,
            "symbol": "BUSDT",
            "native_venue_order_id_lineage": ["stale-stop"],
        },
    )
    assert manager.active("BUSDT") is None
    row = {
        "symbol": "BUSDT",
        "orderId": "stale-stop",
        "orderLinkId": "",
        "orderStatus": "Filled",
        "cumExecQty": "2",
        "triggerPrice": "10.7",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }
    assert manager.is_verified_native_order(row) is False


def test_an_execution_with_nothing_to_reduce_is_ignored_not_an_adoption_failure(
    tmp_path: Path,
) -> None:
    """The venue nets exposure this book never opened.

    REST recovery re-offers every unadopted row each pass, so raising on a
    foreign execution turned one hand-placed close into a permanent error loop
    (ACEUSDT, 2026-08-07: ~250 adoption failures a minute for four hours).
    """

    kernel, clock = _open_position(tmp_path, signed_qty=2.0)
    manager, _client = _manager(kernel, clock)
    # A hand-placed BUY while this book is long: nothing here to reduce.
    adding = {
        "symbol": "BUSDT",
        "orderId": "hand-placed-buy",
        "orderLinkId": "",
        "side": "Buy",
        "execQty": "50",
        "execPrice": "10.0",
        "execId": "foreign-exec-1",
        "execTime": "2000",
    }

    assert manager.adopt_execution(adding, local_receive_ts_ns=clock.wall_time_ns()) == ()
    assert manager.last_error is None or "foreign-exec-1" not in str(manager.last_error)
    assert kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert "foreign-exec-1" not in kernel.state().executions


def test_a_hand_placed_close_larger_than_the_book_reduces_it_to_flat(tmp_path: Path) -> None:
    """Only this book's share is booked; the rest of the venue row is foreign."""

    kernel, clock = _open_position(tmp_path, signed_qty=2.0)
    manager, _client = _manager(kernel, clock)
    closing = {
        "symbol": "BUSDT",
        "orderId": "hand-placed-close",
        "orderLinkId": "",
        "side": "Sell",
        "execQty": "52",
        "execPrice": "10.5",
        "execId": "foreign-exec-2",
        "execTime": "2000",
    }

    manager.adopt_execution(closing, local_receive_ts_ns=clock.wall_time_ns())

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert not state.working_order_ids


def test_a_flat_symbol_clears_only_its_own_health_message(tmp_path: Path) -> None:
    """``last_error`` is one account-wide field written by several conditions.

    It gates all new exposure. Clearing it because SOME terminal status landed
    on SOME flat symbol threw away a live warning about a different symbol that
    still held an unproven stop — and health then passed, admitting fresh risk.
    """

    kernel, clock = _open_position(tmp_path, signed_qty=2.0)
    manager, _client = _manager(kernel, clock)
    manager.adopt_execution(
        {
            "symbol": "BUSDT",
            "orderId": "hand-placed-close",
            "orderLinkId": "",
            "side": "Sell",
            "execQty": "2",
            "execPrice": "10.5",
            "execId": "flatten-exec-1",
            "execTime": "2000",
        },
        local_receive_ts_ns=clock.wall_time_ns(),
    )
    assert kernel.state().positions["BUSDT"].signed_qty == 0.0
    flat_command = next(
        command_id
        for command_id, order in kernel.state().orders.items()
        if order.symbol == "BUSDT"
    )

    # A warning about a different symbol survives BUSDT going terminal.
    manager.last_error = "entry-attached stop unverified for ZUSDT: position_not_visible"
    manager.observe_terminal_status(command_id=flat_command, status="cancelled")
    assert manager.last_error == "entry-attached stop unverified for ZUSDT: position_not_visible"

    # Its own symbol's message is still cleared by its own flatness.
    manager.last_error = "native protection cancelled unfilled for BUSDT: orderId=venue-9"
    manager.observe_terminal_status(command_id=flat_command, status="cancelled")
    assert manager.last_error == ""


def _protection_row(
    *,
    symbol: str | None,
    status: str,
    native: bool,
    ts_ns: int,
) -> dict[str, object]:
    metadata: dict[str, object] = {"native_exchange": native}
    if symbol is not None:
        metadata["symbol"] = symbol
    return {"status": status, "local_receive_ts_ns": ts_ns, "metadata": metadata}


def _reference_active(state, symbol: str):
    """The scan the per-symbol index replaced, kept here as the oracle."""

    matches = [
        (key, protection)
        for key, protection in state.protections.items()
        if str(protection.get("status") or "") in {"active", "triggering"}
        and bool((protection.get("metadata") or {}).get("native_exchange"))
        and str((protection.get("metadata") or {}).get("symbol") or symbol).upper() == symbol
    ]
    return matches[-1] if matches else None


def _reference_latest(state, symbol: str):
    matches = [
        (key, protection)
        for key, protection in state.protections.items()
        if bool((protection.get("metadata") or {}).get("native_exchange"))
        and str((protection.get("metadata") or {}).get("symbol") or symbol).upper() == symbol
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: int(item[1].get("local_receive_ts_ns") or 0))


def _state_with(rows: dict[str, dict[str, object]]):
    from liquidity_migration.account.account_contracts import AccountState

    state = AccountState()
    state.protections.update(rows)
    return state


def test_the_native_protection_index_answers_exactly_what_the_scan_did() -> None:
    """Protections are never pruned, so both lookups grew without bound.

    1,391 of them on the demo book after a day of trading, against 200 that
    morning, and a profile of the reconcile put 16% of its time in these two
    scans. The index has to return the identical row, including which one wins
    a tie -- ``active`` takes the last in state order, ``latest`` takes the
    newest timestamp.
    """

    rows: dict[str, dict[str, object]] = {}
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for i in range(240):
        symbol = symbols[i % len(symbols)]
        rows[f"p{i:04d}"] = _protection_row(
            symbol=symbol,
            status=["active", "triggering", "cancelled", "software_flat_requested"][i % 4],
            native=(i % 5 != 0),
            ts_ns=1_000 + (i * 7) % 991,
        )
    state = _state_with(rows)

    for symbol in [*symbols, "ABSENTUSDT"]:
        assert (
            BybitNativeProtectionManager._active_from_state(state, symbol)
            == _reference_active(state, symbol)
        ), symbol
        assert (
            BybitNativeProtectionManager._latest_native_protection_from_state(state, symbol)
            == _reference_latest(state, symbol)
        ), symbol


def test_a_native_protection_without_its_own_symbol_falls_back_to_the_scan() -> None:
    """Such a row matches whichever symbol is asked about, which no bucket can hold.

    Not expected to occur; the index detects it and stands down rather than
    quietly answering a different question.
    """

    from liquidity_migration.venue.venue_protection import _native_protection_index

    rows = {
        "with-symbol": _protection_row(symbol="BTCUSDT", status="active", native=True, ts_ns=10),
        "no-symbol": _protection_row(symbol=None, status="active", native=True, ts_ns=20),
    }
    state = _state_with(rows)

    assert _native_protection_index(state) is None, "the index must refuse this state"
    for symbol in ("BTCUSDT", "ETHUSDT"):
        assert (
            BybitNativeProtectionManager._active_from_state(state, symbol)
            == _reference_active(state, symbol)
        )
        assert (
            BybitNativeProtectionManager._latest_native_protection_from_state(state, symbol)
            == _reference_latest(state, symbol)
        )


def test_the_index_is_rebuilt_when_the_committed_state_moves() -> None:
    """It is keyed on state identity; a commit publishes a new state object."""

    first = _state_with({"a": _protection_row(symbol="BTCUSDT", status="active", native=True, ts_ns=1)})
    assert BybitNativeProtectionManager._active_from_state(first, "BTCUSDT") is not None

    second = _state_with({"b": _protection_row(symbol="BTCUSDT", status="cancelled", native=True, ts_ns=2)})
    assert BybitNativeProtectionManager._active_from_state(second, "BTCUSDT") is None, (
        "a stale index would still be reporting the previous state's protection"
    )
    assert BybitNativeProtectionManager._active_from_state(first, "BTCUSDT") is not None
