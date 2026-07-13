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
from liquidity_migration.account_reconcile import BybitAccountReconciler
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.venue_protection import BybitNativeProtectionManager


def _kernel(tmp_path: Path, clock: VirtualClock) -> tuple[AccountExecutionKernel, str]:
    kernel = AccountExecutionKernel(tmp_path, account_id="reconcile-account", clock=clock, id_seed="reconcile")
    result = kernel.submit_targets(
        batch_id="batch-1",
        market_inputs=[MarketInputRef("book-1", "BUSDT", 900, 1_000, 10.0)],
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
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet", 950),
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 20.0, 10.0),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
    )
    command_id = result.commands[0].command_id
    kernel.record_ack(
        command_id=command_id,
        accepted=True,
        venue_order_id="venue-1",
        exchange_ts_ns=1_100,
        local_ack_ts_ns=1_101,
    )
    return kernel, command_id


class Client:
    demo = True

    def __init__(self, command_id: str, *, venue_positions: list[dict[str, str]] | None = None) -> None:
        self.command_id = command_id
        self.venue_positions = venue_positions if venue_positions is not None else [
            {"symbol": "BUSDT", "side": "Buy", "size": "1"}
        ]

    def get_trade_history(self, **params: object):
        assert params["order_link_id"] == self.command_id
        return [{
            "orderLinkId": self.command_id,
            "orderId": "venue-1",
            "execId": "exec-rest-1",
            "execQty": "1",
            "execPrice": "10.1",
            "execFee": "0.001",
            "execTime": "2",
            "side": "Buy",
            "seq": "8",
        }]

    def get_order_history(self, **params: object):
        assert params["order_link_id"] == self.command_id
        return [{
            "orderLinkId": self.command_id,
            "orderStatus": "PartiallyFilledCanceled",
            "cumExecQty": "1",
            "updatedTime": "3",
        }]

    def get_positions(self, **params: object):
        assert params == {"settle_coin": "USDT"}
        return self.venue_positions


def test_rest_reconcile_recovers_dropped_execution_then_matches_venue_truth(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _kernel(tmp_path, clock)
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=Client(command_id),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    report = reconciler.reconcile_once()
    assert report.healthy
    assert report.execution_rows_observed == 1
    assert report.order_rows_observed == 1
    assert kernel.state().positions["BUSDT"].signed_qty == pytest.approx(1.0)
    assert kernel.state().orders[command_id].status == "partially_filled_cancelled"
    assert kernel.state().venue_snapshots[report.snapshot_key]["healthy"] is True
    reconciler.require_recent_healthy(max_age_ns=1)
    reconciler.require_recent_symbols_consistent(["BUSDT"], max_age_ns=1)


def test_reconcile_records_and_fails_on_position_mismatch(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _kernel(tmp_path, clock)
    client = Client(command_id, venue_positions=[])
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=client,
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    report = reconciler.reconcile_once()
    assert not report.healthy
    assert report.mismatches[0].startswith("BUSDT:venue=0:reconstructed=1")
    with pytest.raises(RuntimeError, match="reconciliation unhealthy"):
        reconciler.require_recent_healthy(max_age_ns=1)
    with pytest.raises(RuntimeError, match="position truth contradicts reduction"):
        reconciler.require_recent_symbols_consistent(["BUSDT"], max_age_ns=1)


def test_dual_side_venue_position_fails_closed_for_net_position_kernel(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="dual", clock=clock)

    class DualClient:
        demo = True

        def get_positions(self, **_params: object):
            return [
                {"symbol": "BUSDT", "side": "Buy", "size": "1"},
                {"symbol": "BUSDT", "side": "Sell", "size": "1"},
            ]

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=DualClient(),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    report = reconciler.reconcile_once()
    assert not report.healthy
    assert report.mismatches == ("BUSDT:dual_side_position_not_supported",)


def test_rest_reconcile_recovers_native_stop_execution_missed_by_ws(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="native-rest", clock=clock, id_seed="native-rest")
    result = kernel.submit_targets(
        batch_id="open",
        market_inputs=[MarketInputRef("book", "BUSDT", 1_000_000_000, 1_100_000_000, 10.0)],
        targets=[DesiredTarget(
            decision_key="open-d",
            target_key="long/strategy/trade/BUSDT",
            sleeve="long",
            strategy_id="strategy",
            component_id="trade",
            symbol="BUSDT",
            signed_qty=1.0,
            reference_price=10.0,
            leverage=10.0,
        )],
        risk_snapshot=AccountRiskSnapshot(1_000.0, 900.0, "wallet", 1_500_000_000),
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 100.0, 10.0),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1),
        },
    )
    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="entry-order",
        exchange_ts_ns=1_600_000_000,
        local_ack_ts_ns=1_610_000_000,
    )
    kernel.record_fill(
        command_id=command.command_id,
        execution_id="entry-fill",
        signed_qty=1.0,
        price=10.0,
        fee_usdt=0.01,
        exchange_ts_ns=1_620_000_000,
        local_receive_ts_ns=1_625_000_000,
    )

    class NativeClient:
        demo = True

        def set_trading_stop(self, **_params: object):
            return {}

        def get_trade_history(self, **params: object):
            assert params == {"symbol": "BUSDT", "limit": 50}
            return [{
                "symbol": "BUSDT",
                "orderLinkId": "",
                "orderId": "native-stop-order",
                "execId": "native-stop-fill",
                "execQty": "1",
                "execPrice": "9",
                "execFee": "0.02",
                    # The exchange clock can precede the local timestamp taken
                    # after set_trading_stop returns. This valid fill is 1 ms
                    # before local activation and must survive bounded skew.
                    "execTime": "1999",
                    "side": "Sell",
                    "createType": "CreateByStopLoss",
                    "stopOrderType": "StopLoss",
                }]

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return []

    client = NativeClient()
    rules = {
        "BUSDT": InstrumentRules(
            "BUSDT", 0.1, 0.1, 1.0, tick_size=0.1, environment="demo"
        ),
    }
    manager = BybitNativeProtectionManager(
        kernel=kernel,
        client=client,
        instrument_rules=rules,
        fallback_stop_fraction=0.07,
        clock=clock,
    )
    manager.sync("BUSDT")
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=client,
        instrument_rules=rules,
        native_protection_manager=manager,
        clock=clock,
    )

    report = reconciler.reconcile_once()

    assert report.healthy
    assert report.execution_rows_observed == 1
    assert kernel.state().positions["BUSDT"].signed_qty == 0.0
    assert len(kernel.state().pnl) == 1


def test_rest_reconcile_queries_adopted_native_order_by_venue_id(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(
        tmp_path,
        account_id="native-partial-rest",
        clock=clock,
        id_seed="native-partial-rest",
    )
    result = kernel.submit_targets(
        batch_id="open",
        market_inputs=[MarketInputRef("book", "BUSDT", 1_000_000_000, 1_100_000_000, 10.0)],
        targets=[DesiredTarget(
            decision_key="open-d",
            target_key="long/strategy/trade/BUSDT",
            sleeve="long",
            strategy_id="strategy",
            component_id="trade",
            symbol="BUSDT",
            signed_qty=2.0,
            reference_price=10.0,
            leverage=10.0,
        )],
        risk_snapshot=AccountRiskSnapshot(1_000.0, 900.0, "wallet", 1_500_000_000),
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 100.0, 10.0),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1),
        },
    )
    entry = result.commands[0]
    kernel.record_ack(
        command_id=entry.command_id,
        accepted=True,
        venue_order_id="entry-order",
        exchange_ts_ns=1_600_000_000,
        local_ack_ts_ns=1_610_000_000,
    )
    kernel.record_fill(
        command_id=entry.command_id,
        execution_id="entry-fill",
        signed_qty=2.0,
        price=10.0,
        fee_usdt=0.01,
        exchange_ts_ns=1_620_000_000,
        local_receive_ts_ns=1_625_000_000,
    )

    class NativeInstallClient:
        demo = True

        def set_trading_stop(self, **_params: object):
            return {}

    rules = {
        "BUSDT": InstrumentRules(
            "BUSDT", 0.1, 0.1, 1.0, tick_size=0.1, environment="demo"
        ),
    }
    manager = BybitNativeProtectionManager(
        kernel=kernel,
        client=NativeInstallClient(),
        instrument_rules=rules,
        fallback_stop_fraction=0.07,
        clock=clock,
    )
    manager.sync("BUSDT")
    BybitAccountExecutionConsumer(
        kernel=kernel,
        native_protection_manager=manager,
        clock=clock,
    ).on_execution({"data": [{
        "symbol": "BUSDT",
        "orderId": "native-partial-order",
        "execId": "native-partial-fill",
        "side": "Sell",
        "execQty": "0.5",
        "execPrice": "9.3",
        "execFee": "0.005",
        "execTime": "2100",
        "createType": "CreateByStopLoss",
        "stopOrderType": "StopLoss",
    }]}, local_receive_ts_ns=2_100_000_000)

    class RecoveryClient:
        demo = True

        def get_trade_history(self, **params: object):
            assert params == {
                "symbol": "BUSDT",
                "order_id": "native-partial-order",
                "limit": 100,
            }
            return []

        def get_order_history(self, **params: object):
            assert params == {
                "symbol": "BUSDT",
                "order_id": "native-partial-order",
                "limit": 10,
            }
            return [{
                "symbol": "BUSDT",
                "orderLinkId": "",
                "orderId": "native-partial-order",
                "orderStatus": "Cancelled",
                "cumExecQty": "0.5",
                "updatedTime": "2200",
                "createType": "CreateByStopLoss",
                "stopOrderType": "StopLoss",
            }]

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return [{"symbol": "BUSDT", "side": "Buy", "size": "1.5"}]

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=RecoveryClient(),
        instrument_rules=rules,
        clock=clock,
    )
    report = reconciler.reconcile_once()

    assert report.healthy
    adopted = [
        order
        for order in kernel.state().orders.values()
        if order.venue_order_id == "native-partial-order"
    ]
    assert len(adopted) == 1
    assert adopted[0].status == "partially_filled_cancelled"
    assert kernel.state().working_signed_qty("BUSDT") == 0.0
