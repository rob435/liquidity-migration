from __future__ import annotations

import re
from pathlib import Path

import pytest

from liquidity_migration.venue.account_execution_stream import BybitAccountExecutionConsumer
from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.venue.account_reconcile import (
    POSITION_HEALTH_MAX_AGE_FLOOR_NS,
    VENUE_SNAPSHOT_CHECKPOINT_INTERVAL_NS,
    AccountReconciliationStaleError,
    BybitAccountReconciler,
)
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.venue.venue_protection import BybitNativeProtectionManager


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
    realm = "demo"

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


def test_protection_reverification_runs_for_symbols_a_mismatch_does_not_implicate(
    tmp_path: Path,
) -> None:
    """B6: one illegible symbol must not suspend stop proof for the whole account."""

    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="scoped-skip", clock=clock)

    class MismatchClient(_NoOpenOrdersClient):
        demo = True
        realm = "demo"

        def get_positions(self, **_params: object):
            # CUSDT exists only at the venue, so it is exposure this book does
            # not own and its venue quantity is not a legible basis for a stop
            # plan. DUSDT is flat on both sides.
            return [
                {"symbol": "CUSDT", "side": "Buy", "size": "3"},
                {"symbol": "DUSDT", "side": "Buy", "size": "0"},
            ]

    calls: list[frozenset[str]] = []

    class RecordingNativeManager:
        def reconcile_venue_positions(
            self, rows: object, *, skip_symbols: frozenset[str] = frozenset()
        ) -> None:
            calls.append(frozenset(skip_symbols))

        def is_verified_native_order(self, _candidate: object) -> bool:
            return False

    report = BybitAccountReconciler(
        kernel=kernel,
        client=MismatchClient(),
        instrument_rules={},
        native_protection_manager=RecordingNativeManager(),
        clock=clock,
    ).reconcile_once()

    # Exposure this book does not own is recorded, not a fault.
    assert report.healthy, report.mismatches
    assert report.foreign_positions == {"CUSDT": 3.0}
    # The call happened at all: one skipped symbol must not skip it entirely.
    assert calls == [frozenset({"CUSDT"})]


def test_protection_reverification_skips_a_symbol_with_an_ambiguous_submission(
    tmp_path: Path,
) -> None:
    """An in-flight ambiguous entry makes only its own symbol illegible."""

    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(
        tmp_path, account_id="ambiguous-scope", clock=clock, id_seed="ambiguous"
    )
    result = kernel.submit_targets(
        batch_id="batch-1",
        market_inputs=[MarketInputRef("book-1", "BUSDT", 900, 1_000, 10.0)],
        targets=[
            DesiredTarget(
                decision_key="d1",
                target_key="long/main/BUSDT",
                sleeve="long",
                strategy_id="long-v1",
                component_id="main",
                symbol="BUSDT",
                signed_qty=2.0,
                reference_price=10.0,
                leverage=10.0,
            )
        ],
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet", 950),
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 20.0, 10.0),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
    )
    command_id = result.commands[0].command_id
    kernel.record_submission_attempt(command_id=command_id, adapter_name="bybit_demo")

    class FlatClient(_NoOpenOrdersClient):
        demo = True
        realm = "demo"

        def get_positions(self, **_params: object):
            return []

        def get_trade_history(self, **_params: object):
            return []

        def get_order_history(self, **_params: object):
            return []

    calls: list[frozenset[str]] = []

    class RecordingNativeManager:
        def reconcile_venue_positions(
            self, rows: object, *, skip_symbols: frozenset[str] = frozenset()
        ) -> None:
            calls.append(frozenset(skip_symbols))

        def is_verified_native_order(self, _candidate: object) -> bool:
            return False

    report = BybitAccountReconciler(
        kernel=kernel,
        client=FlatClient(),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        native_protection_manager=RecordingNativeManager(),
        clock=clock,
    ).reconcile_once()

    assert not report.healthy
    assert any("ambiguous_submission_unresolved" in text for text in report.mismatches)
    assert calls == [frozenset({"BUSDT"})]


def test_dual_side_venue_position_fails_closed_for_net_position_kernel(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="dual", clock=clock)

    class DualClient(_NoOpenOrdersClient):
        demo = True
        realm = "demo"

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
        realm = "demo"

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
        realm = "demo"

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


def test_reconciliation_marks_ack_lost_entry_without_venue_evidence_unhealthy(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(
        tmp_path,
        account_id="ambiguous-entry",
        clock=clock,
    )
    result = kernel.submit_targets(
        batch_id="ambiguous-entry",
        market_inputs=[MarketInputRef("book-1", "BUSDT", 900, 1_000, 10.0)],
        targets=[
            DesiredTarget(
                decision_key="ambiguous-entry",
                target_key="long/ambiguous/BUSDT",
                sleeve="long",
                strategy_id="long-v1",
                component_id="ambiguous",
                symbol="BUSDT",
                signed_qty=2.0,
                reference_price=10.0,
                leverage=10.0,
            )
        ],
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet", 950),
        risk_policy=AccountRiskPolicy(
            100.0,
            100.0,
            100.0,
            20.0,
            10.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0),
        },
    )
    command_id = result.commands[0].command_id
    kernel.record_submission_attempt(
        command_id=command_id,
        adapter_name="bybit_demo",
    )

    class NoEvidenceClient(_NoOpenOrdersClient):
        demo = True
        realm = "demo"

        def get_trade_history(self, **params: object):
            assert params["order_link_id"] == command_id
            return []

        def get_order_history(self, **params: object):
            assert params["order_link_id"] == command_id
            return []

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return []

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=NoEvidenceClient(),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0),
        },
        clock=clock,
    )
    report = reconciler.reconcile_once()

    assert not report.healthy
    assert report.mismatches == (
        "BUSDT:ambiguous_submission_unresolved:"
        f"command={command_id}:attempts=1",
    )
    with pytest.raises(RuntimeError, match="ambiguous_submission_unresolved"):
        reconciler.require_recent_healthy(max_age_ns=1)


@pytest.mark.parametrize("conditional", [False, True])
def test_reconciliation_detects_unowned_order_appearing_after_clean_start(
    tmp_path: Path,
    conditional: bool,
) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="continuous-order-ownership", clock=clock)

    class MutableOrderClient:
        demo = True
        realm = "demo"

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

    # The account owner shares the venue account with the owner trading by
    # hand, so a venue order it does not own is recorded and left alone rather
    # than stopping the fleet (decision 2026-08-07).
    assert report.healthy, report.mismatches
    snapshot = kernel.state().venue_snapshots[report.snapshot_key]
    ownership = snapshot["metadata"]["venue_order_ownership"]
    assert ownership["status"] == "unowned"
    assert ownership["unique_orders_observed"] == 1
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
        realm = "demo"

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
        realm = "demo"

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
        realm = "demo"

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
        realm = "demo"

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return []

        def get_open_orders(self, **params: object):
            return [row] if params.get("order_filter") == "StopOrder" else [row]

    class VerifiedNativeManager:
        def reconcile_venue_positions(
            self, rows: object, *, skip_symbols: object = frozenset()
        ) -> None:
            assert rows == []
            assert skip_symbols == frozenset()

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


@pytest.mark.parametrize(
    ("breaches_only", "expected_recovery_authority"),
    [(True, True), (False, False)],
)
def test_reconciliation_propagates_only_structured_native_breach_authority(
    tmp_path: Path,
    breaches_only: bool,
    expected_recovery_authority: bool,
) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(
        tmp_path,
        account_id="structured-native-breach",
        clock=clock,
    )

    class FlatClient(_NoOpenOrdersClient):
        demo = True
        realm = "demo"

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return []

    class NativeManager:
        def reconcile_venue_positions(
            self, rows: object, *, skip_symbols: object = frozenset()
        ) -> None:
            assert rows == []
            assert skip_symbols == frozenset()
            error = RuntimeError("provider text may mention NativeProtectionBreachError")
            error.breaches_only = breaches_only  # type: ignore[attr-defined]
            raise error

        def is_verified_native_order(self, _candidate: object) -> bool:
            return False

    report = BybitAccountReconciler(
        kernel=kernel,
        client=FlatClient(),
        instrument_rules={},
        native_protection_manager=NativeManager(),
        clock=clock,
    ).reconcile_once()

    assert report.healthy is False
    assert len(report.mismatches) == 1
    assert report.mismatches[0].startswith("native_protection:RuntimeError:")
    assert report.native_protection_breach_only is expected_recovery_authority


def test_position_truth_timestamp_is_taken_after_rest_response(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=0)
    kernel = AccountExecutionKernel(tmp_path, account_id="fresh-position-truth", clock=clock)

    class DelayedPositionClient(_NoOpenOrdersClient):
        demo = True
        realm = "demo"

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
        realm = "demo"

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
        realm = "demo"
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
    # Well short of the checkpoint floor: the second snapshot can only be the
    # position change, not the heartbeat coming due.
    clock.advance_ns(VENUE_SNAPSHOT_CHECKPOINT_INTERVAL_NS // 2)
    client.venue_positions = [{"symbol": "BUSDT", "side": "Buy", "size": "1"}]

    changed = reconciler.reconcile_once()

    snapshots = [
        event
        for event in kernel.journal.events()
        if event.event_type == "venue_snapshot"
    ]
    assert len(snapshots) == 2
    # The book owns nothing in BUSDT, so the venue's position is foreign: a
    # journaled semantic change, not a fault.
    assert changed.healthy, changed.mismatches
    assert changed.foreign_positions == {"BUSDT": 1.0}
    assert list(kernel.state().venue_snapshots) == [changed.snapshot_key]


def test_venue_snapshot_events_carry_no_symbol(tmp_path: Path) -> None:
    """Snapshots are account-wide facts, so their symbol field stays empty.

    ``AccountService._orphan_observed_since_ns`` filters candidate events by
    ``event.symbol == symbol``, so a venue snapshot never matches that clause.
    This pins the shape that clause depends on.
    """

    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=10)
    kernel = AccountExecutionKernel(tmp_path, account_id="symbolless", clock=clock)

    class PositionedClient(_NoOpenOrdersClient):
        demo = True
        realm = "demo"

        def get_positions(self, **params: object):
            assert params == {"settle_coin": "USDT"}
            return [{"symbol": "BUSDT", "side": "Buy", "size": "1"}]

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=PositionedClient(),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    reconciler.reconcile_once()

    snapshots = [
        event
        for event in kernel.journal.events()
        if event.event_type == "venue_snapshot"
    ]
    assert snapshots
    assert {event.symbol for event in snapshots} == {""}


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
        realm = "demo"

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
        realm = "demo"

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
        realm = "demo"

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


class _MainnetClient(Client):
    demo = False
    realm = "mainnet"


def test_reconciler_refuses_a_client_that_names_no_parsable_realm() -> None:
    class Realmless:
        demo = True

    class BadRealm:
        demo = True
        realm = "paper"

    for client in (object(), Realmless(), BadRealm()):
        with pytest.raises(ValueError, match="naming venue realm"):
            BybitAccountReconciler(kernel=None, client=client, instrument_rules={})  # type: ignore[arg-type]


def test_mainnet_reconcile_matches_demo_except_for_the_realm_label(tmp_path: Path) -> None:
    """Same decisions, same journal payload; only the realm-derived labels move."""

    reports = {}
    events = {}
    for realm, factory in (("demo", Client), ("mainnet", _MainnetClient)):
        clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
        kernel, command_id = _kernel(tmp_path / realm, clock)
        reconciler = BybitAccountReconciler(
            kernel=kernel,
            client=factory(command_id),
            instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
            clock=clock,
        )
        assert reconciler.realm.value == realm
        reports[realm] = reconciler.reconcile_once()
        events[realm] = kernel.state().venue_snapshots[reports[realm].snapshot_key]

    assert reports["demo"].snapshot_key.startswith("bybit-demo-position:")
    assert reports["mainnet"].snapshot_key.startswith("bybit-mainnet-position:")
    assert events["demo"]["metadata"]["source"] == "bybit_demo_rest_reconcile"
    assert events["mainnet"]["metadata"]["source"] == "bybit_mainnet_rest_reconcile"
    # The digest covers positions, ownership counts and mismatches. None of those
    # names a realm on a clean pass, so the two agree here -- but a mismatch that
    # quotes a realm-labelled venue fault does move it; see the next test.
    assert reports["demo"].snapshot_key.split(":")[1] == reports["mainnet"].snapshot_key.split(":")[1]
    for realm in ("demo", "mainnet"):
        assert reports[realm].healthy
        assert reports[realm].mismatches == ()
        assert dict(reports[realm].venue_positions) == {"BUSDT": 1.0}
        assert dict(reports[realm].reconstructed_positions) == {"BUSDT": 1.0}
    # ``source`` is the only realm-derived name inside metadata, so drop that one
    # key rather than the whole dict: metadata is where ``venue_order_ownership``
    # records that the per-cycle ownership read -- the call that used to refuse a
    # non-demo client outright -- ran clean on mainnet too.
    normalized = {}
    for realm in ("demo", "mainnet"):
        event = dict(events[realm])
        metadata = dict(event["metadata"])
        del metadata["source"]
        assert metadata["venue_order_ownership"]["status"] == "verified"
        event["metadata"] = metadata
        del event["snapshot_key"]
        normalized[realm] = event
    assert normalized["demo"] == normalized["mainnet"]


def test_an_unreadable_order_book_puts_the_realm_into_the_mismatch_and_digest(
    tmp_path: Path,
) -> None:
    """``mismatches`` is hashed into the snapshot key, and it quotes venue faults.

    So the two realms agree on the digest only while no mismatch names one. The
    demo text is unchanged; a mainnet operator reading the journal sees mainnet.
    """

    keys = {}
    for realm, factory in (("demo", Client), ("mainnet", _MainnetClient)):
        clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
        kernel, command_id = _kernel(tmp_path / realm, clock)
        client = factory(command_id)
        client.get_open_orders = _raise_venue_500  # type: ignore[method-assign]
        reconciler = BybitAccountReconciler(
            kernel=kernel,
            client=client,
            instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
            clock=clock,
        )
        report = reconciler.reconcile_once()
        assert not report.healthy
        assert report.mismatches == (
            "venue_order_ownership:inspection_failed:RuntimeError:"
            f"Bybit {realm} could not prove venue order ownership: "
            "all-kinds open-order query failed: RuntimeError: venue 500",
        )
        keys[realm] = report.snapshot_key.split(":")[1]

    assert keys["demo"] != keys["mainnet"]


def _raise_venue_500(**_params: object) -> list[dict[str, str]]:
    raise RuntimeError("venue 500")


def test_mainnet_position_row_faults_name_the_mainnet_realm(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _kernel(tmp_path, clock)
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=_MainnetClient(command_id, venue_positions=[{"side": "Buy", "size": "1"}]),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="Bybit mainnet position row 0 lacks symbol"):
        reconciler.reconcile_once()


def test_mainnet_hedge_mode_position_stays_a_named_unhealthy_mismatch(tmp_path: Path) -> None:
    """Bybit hedge mode is unmodelled; both realms refuse it by name, never silently."""

    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _kernel(tmp_path, clock)
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=_MainnetClient(
            command_id,
            venue_positions=[
                {"symbol": "BUSDT", "side": "Buy", "size": "1"},
                {"symbol": "BUSDT", "side": "Sell", "size": "1"},
            ],
        ),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    report = reconciler.reconcile_once()

    assert not report.healthy
    assert "BUSDT:dual_side_position_not_supported" in report.mismatches


def test_demo_reconcile_labels_and_fault_text_are_pinned(tmp_path: Path) -> None:
    """The demo journal key, source, and venue-fault text this change touched."""

    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _kernel(tmp_path, clock)
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=Client(command_id, venue_positions=[]),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    report = reconciler.reconcile_once()

    assert report.snapshot_key.startswith("bybit-demo-position:")
    snapshot = kernel.state().venue_snapshots[report.snapshot_key]
    assert snapshot["metadata"]["source"] == "bybit_demo_rest_reconcile"
    assert report.mismatches == ("BUSDT:venue=0:reconstructed=1:unbacked=1:tol=0.05",)
    with pytest.raises(RuntimeError, match="account reconciliation unhealthy: BUSDT:venue=0"):
        reconciler.require_recent_healthy(max_age_ns=1)

    for positions, message in (
        ("nope", "Bybit demo position query returned a non-list payload"),
        ([1], "Bybit demo position query returned a non-object row at index 0"),
        ([{"side": "Buy", "size": "1"}], "Bybit demo position row 0 lacks symbol"),
        ([{"symbol": "BUSDT", "side": "Buy"}], "Bybit demo position row 0 size must be numeric"),
        (
            [{"symbol": "BUSDT", "side": "Buy", "size": "nan"}],
            "Bybit demo position row 0 size must be finite",
        ),
        (
            [{"symbol": "BUSDT", "side": "Buy", "size": "-1"}],
            "Bybit demo position row 0 size must be non-negative",
        ),
        (
            [{"symbol": "BUSDT", "side": "Up", "size": "1"}],
            "Bybit demo position row 0 has invalid side 'Up'",
        ),
    ):
        reconciler.client = Client(command_id, venue_positions=positions)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match=re.escape(message)):
            reconciler.reconcile_once()
