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
from liquidity_migration.account_reconcile import (
    POSITION_HEALTH_MAX_AGE_FLOOR_NS,
    VENUE_SNAPSHOT_CHECKPOINT_INTERVAL_NS,
    AccountReconciliationStaleError,
    BybitAccountReconciler,
)
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


class _NoOpenOrdersClient:
    def get_open_orders(self, **params: object):
        assert params in (
            {"settle_coin": "USDT"},
            {"settle_coin": "USDT", "order_filter": "StopOrder"},
        )
        return []


class Client(_NoOpenOrdersClient):
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
    observed_executions: list[str] = []
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=Client(command_id),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        fill_observer=observed_executions.append,
        clock=clock,
    )
    report = reconciler.reconcile_once()
    assert report.healthy
    assert report.execution_rows_observed == 1
    assert report.order_rows_observed == 1
    assert kernel.state().positions["BUSDT"].signed_qty == pytest.approx(1.0)
    assert kernel.state().orders[command_id].status == "partially_filled_cancelled"
    assert observed_executions == ["exec-rest-1"]
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

    class DualClient(_NoOpenOrdersClient):
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


@pytest.mark.parametrize(
    ("venue_positions", "error"),
    [
        (None, "returned a non-list payload"),
        ({"symbol": "BUSDT"}, "returned a non-list payload"),
        ([None], "returned a non-object row at index 0"),
        ([{"symbol": "", "side": "Buy", "size": "1"}], "row 0 lacks symbol"),
        ([{"symbol": "BUSDT", "side": "Buy", "size": None}], "row 0 size must be numeric"),
        ([{"symbol": "BUSDT", "side": "Buy", "size": "not-a-number"}], "row 0 size must be numeric"),
        ([{"symbol": "BUSDT", "side": "Buy", "size": "NaN"}], "row 0 size must be finite"),
        ([{"symbol": "BUSDT", "side": "Buy", "size": "Infinity"}], "row 0 size must be finite"),
        ([{"symbol": "BUSDT", "side": "Buy", "size": "-1"}], "row 0 size must be non-negative"),
        ([{"symbol": "BUSDT", "side": "", "size": "1"}], "row 0 has invalid side"),
        ([{"symbol": "BUSDT", "side": "Both", "size": "1"}], "row 0 has invalid side"),
        ([{"symbol": "BUSDT", "side": "Both", "size": "0"}], "row 0 has invalid side"),
    ],
)
def test_malformed_venue_position_snapshot_fails_closed(
    tmp_path: Path,
    venue_positions: object,
    error: str,
) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="strict-position-response", clock=clock)

    class MalformedClient(_NoOpenOrdersClient):
        demo = True

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return venue_positions

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=MalformedClient(),
        instrument_rules={},
        clock=clock,
    )

    with pytest.raises(RuntimeError, match=error):
        reconciler.reconcile_once()

    assert reconciler.last_report is None
    assert not kernel.state().venue_snapshots


def test_canonical_zero_venue_position_row_is_valid_flat_truth(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="canonical-flat-position", clock=clock)

    class FlatClient(_NoOpenOrdersClient):
        demo = True

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return [{"symbol": "BUSDT", "side": "", "size": "0"}]

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=FlatClient(),
        instrument_rules={},
        clock=clock,
    )

    report = reconciler.reconcile_once()

    assert report.healthy
    assert report.venue_positions == {}
    assert report.mismatches == ()


@pytest.mark.parametrize("conditional", [False, True])
def test_reconciliation_detects_unowned_order_appearing_after_clean_start(
    tmp_path: Path,
    conditional: bool,
) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="continuous-order-ownership", clock=clock)

    class MutableOrderClient:
        demo = True

        def __init__(self) -> None:
            self.all_kinds: list[dict[str, str]] = []
            self.conditional: list[dict[str, str]] = []
            self.open_order_calls: list[dict[str, object]] = []

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return []

        def get_open_orders(self, **params: object):
            self.open_order_calls.append(dict(params))
            if params.get("order_filter") == "StopOrder":
                return list(self.conditional)
            return list(self.all_kinds)

    client = MutableOrderClient()
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=client,
        instrument_rules={},
        clock=clock,
    )
    assert reconciler.reconcile_once().healthy

    row = {
        "symbol": "BUSDT",
        "orderId": "post-start-stray",
        "orderLinkId": "manual-order",
        "orderStatus": "Untriggered" if conditional else "New",
        "stopOrderType": "StopLoss" if conditional else "",
        "triggerPrice": "0.1" if conditional else "",
    }
    client.all_kinds = [row]
    client.conditional = [row] if conditional else []
    clock.advance_ns(1)

    report = reconciler.reconcile_once()

    assert not report.healthy
    assert len(report.mismatches) == 1
    assert report.mismatches[0].startswith("BUSDT:unowned_venue_order:")
    assert ("conditional" if conditional else "regular") in report.mismatches[0]
    with pytest.raises(RuntimeError, match="position truth contradicts reduction"):
        reconciler.require_recent_symbols_consistent(["BUSDT"], max_age_ns=0)
    assert client.open_order_calls == [
        {"settle_coin": "USDT"},
        {"settle_coin": "USDT", "order_filter": "StopOrder"},
        {"settle_coin": "USDT"},
        {"settle_coin": "USDT", "order_filter": "StopOrder"},
    ]


def test_reconciliation_blocks_when_open_order_snapshot_is_unknown(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="unknown-order-ownership", clock=clock)

    class FailedOrderClient:
        demo = True

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return []

        def get_open_orders(self, **params: object):
            if params.get("order_filter") == "StopOrder":
                raise RuntimeError("conditional query unavailable")
            return []

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=FailedOrderClient(),
        instrument_rules={},
        clock=clock,
    )

    report = reconciler.reconcile_once()

    assert not report.healthy
    assert len(report.mismatches) == 1
    assert report.mismatches[0].startswith("venue_order_ownership:inspection_failed:")
    with pytest.raises(RuntimeError, match="position truth contradicts reduction"):
        reconciler.require_recent_symbols_consistent(["BUSDT"], max_age_ns=0)


def test_reconciliation_accepts_exact_kernel_owned_open_order(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _kernel(tmp_path, clock)

    class OwnedOrderClient:
        demo = True

        def get_trade_history(self, **params: object):
            assert params["order_link_id"] == command_id
            return []

        def get_order_history(self, **params: object):
            assert params["order_link_id"] == command_id
            return []

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return []

        def get_open_orders(self, **params: object):
            if params.get("order_filter") == "StopOrder":
                return []
            return [{
                "symbol": "BUSDT",
                "orderId": "venue-1",
                "orderLinkId": command_id,
                "orderStatus": "New",
            }]

    report = BybitAccountReconciler(
        kernel=kernel,
        client=OwnedOrderClient(),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    ).reconcile_once()

    assert report.healthy
    assert report.mismatches == ()


@pytest.mark.parametrize(
    ("venue_symbol", "error"),
    [
        ("", "lacks symbol"),
        ("ETHUSDT", "different symbol"),
    ],
)
def test_reconciliation_rejects_malformed_kernel_order_identity_match(
    tmp_path: Path,
    venue_symbol: str,
    error: str,
) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _kernel(tmp_path, clock)

    class ContradictoryOrderClient:
        demo = True

        def get_trade_history(self, **_params: object):
            return []

        def get_order_history(self, **_params: object):
            return []

        def get_positions(self, **_params: object):
            return []

        def get_open_orders(self, **params: object):
            if params.get("order_filter") == "StopOrder":
                return []
            return [{
                "symbol": venue_symbol,
                "orderId": "venue-1",
                "orderLinkId": command_id,
                "orderStatus": "New",
            }]

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=ContradictoryOrderClient(),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )

    report = reconciler.reconcile_once()

    assert not report.healthy
    assert report.mismatches[0].startswith("venue_order_ownership:inspection_failed:")
    assert error in report.mismatches[0]
    with pytest.raises(RuntimeError, match="position truth contradicts reduction"):
        reconciler.require_recent_symbols_consistent(["BUSDT"], max_age_ns=0)


def test_reconciliation_accepts_journal_verified_native_open_order(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="verified-native-order", clock=clock)
    kernel.record_venue_snapshot(
        snapshot_key="prior-clean-snapshot",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=0,
        local_receive_ts_ns=1,
    )
    row = {
        "symbol": "BUSDT",
        "orderId": "native-stop-1",
        "orderLinkId": "",
        "orderStatus": "Untriggered",
        "stopOrderType": "StopLoss",
        "triggerPrice": "0.1",
    }

    class NativeOrderClient:
        demo = True

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return []

        def get_open_orders(self, **params: object):
            return [row] if params.get("order_filter") == "StopOrder" else [row]

    class VerifiedNativeManager:
        def reconcile_venue_positions(self, rows: object) -> None:
            assert rows == []

        def is_verified_native_order(self, candidate: object) -> bool:
            return candidate == row

    report = BybitAccountReconciler(
        kernel=kernel,
        client=NativeOrderClient(),
        instrument_rules={},
        native_protection_manager=VerifiedNativeManager(),
        clock=clock,
    ).reconcile_once()

    assert report.healthy
    assert report.mismatches == ()


def test_position_truth_timestamp_is_taken_after_rest_response(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=0)
    kernel = AccountExecutionKernel(tmp_path, account_id="fresh-position-truth", clock=clock)

    class DelayedPositionClient(_NoOpenOrdersClient):
        demo = True

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            clock.advance_ns(9_000_000_000)
            return []

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=DelayedPositionClient(),
        instrument_rules={},
        clock=clock,
    )

    report = reconciler.reconcile_once()

    assert report.observed_ts_ns == clock.wall_time_ns()
    reconciler.require_recent_healthy(max_age_ns=0)


def test_noop_reconciliation_is_fresh_without_growing_journal_until_checkpoint(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=10)
    kernel = AccountExecutionKernel(tmp_path, account_id="bounded-reconcile", clock=clock)

    class FlatClient(_NoOpenOrdersClient):
        demo = True

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return []

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=FlatClient(),
        instrument_rules={},
        clock=clock,
    )

    first = reconciler.reconcile_once()
    clock.advance_ns(2_000_000_000)
    second = reconciler.reconcile_once()

    snapshots = [
        event
        for event in kernel.journal.events()
        if event.event_type == "venue_snapshot"
    ]
    assert len(snapshots) == 1
    assert second.observed_ts_ns > first.observed_ts_ns
    reconciler.require_recent_healthy(max_age_ns=0)

    clock.advance_ns(VENUE_SNAPSHOT_CHECKPOINT_INTERVAL_NS)
    reconciler.reconcile_once()

    snapshots = [
        event
        for event in kernel.journal.events()
        if event.event_type == "venue_snapshot"
    ]
    assert len(snapshots) == 2


def test_reconciliation_semantic_change_is_journaled_immediately(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=10)
    kernel = AccountExecutionKernel(tmp_path, account_id="changed-reconcile", clock=clock)

    class MutableClient(_NoOpenOrdersClient):
        demo = True
        venue_positions: list[dict[str, str]] = []

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return self.venue_positions

    client = MutableClient()
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=client,
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    reconciler.reconcile_once()
    clock.advance_ns(1)
    client.venue_positions = [{"symbol": "BUSDT", "side": "Buy", "size": "1"}]

    changed = reconciler.reconcile_once()

    snapshots = [
        event
        for event in kernel.journal.events()
        if event.event_type == "venue_snapshot"
    ]
    assert len(snapshots) == 2
    assert not changed.healthy
    assert list(kernel.state().venue_snapshots) == [changed.snapshot_key]


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

    class NativeClient(_NoOpenOrdersClient):
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

    class RecoveryClient(_NoOpenOrdersClient):
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


def test_position_health_floor_absorbs_one_slow_reconcile_pass(tmp_path: Path) -> None:
    """A 4-5s report age from ordinary funding-then-position sequencing must not page."""
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _kernel(tmp_path, clock)
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=Client(command_id),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    reconciler.reconcile_once()

    clock.advance_ns(5_000_000_000)
    reconciler.require_recent_healthy(max_age_ns=4_000_000_000)
    reconciler.require_recent_symbols_consistent(["BUSDT"], max_age_ns=4_000_000_000)


def test_position_health_floor_still_fails_a_wedged_reconciler(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _kernel(tmp_path, clock)
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=Client(command_id),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    reconciler.reconcile_once()

    clock.advance_ns(POSITION_HEALTH_MAX_AGE_FLOOR_NS + 1)
    with pytest.raises(AccountReconciliationStaleError, match="is stale"):
        reconciler.require_recent_healthy(max_age_ns=4_000_000_000)
    with pytest.raises(AccountReconciliationStaleError, match="is stale"):
        reconciler.require_recent_symbols_consistent(["BUSDT"], max_age_ns=4_000_000_000)
