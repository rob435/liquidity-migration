"""The reconciler's automatic wedge pass.

A command the venue demonstrably does not hold terminalizes on the CLI's own
evidence ladder, inside an ordinary demo reconcile pass. Mainnet only
classifies: the wedge becomes visible in health while the transition stays an
operator act. Live orders and young commands are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.account.wedged_command_watch import DEFAULT_WEDGE_AFTER_NS
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.venue.account_reconcile import BybitAccountReconciler
from liquidity_migration.venue.wedged_command_resolution import RESOLUTION_REJECTION_KEY

RULES = {"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)}
POLICY = AccountRiskPolicy(100.0, 100.0, 100.0, 20.0, 10.0)


def _wedged_kernel(
    tmp_path: Path,
    clock: VirtualClock,
    *,
    attempted: bool,
) -> tuple[AccountExecutionKernel, str]:
    kernel = AccountExecutionKernel(
        tmp_path, account_id="wedge-account", clock=clock, id_seed="wedge"
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
        risk_policy=POLICY,
        instrument_rules=RULES,
    )
    command_id = result.commands[0].command_id
    if attempted:
        kernel.record_submission_attempt(command_id=command_id, adapter_name="bybit_demo")
    clock.advance_ns(DEFAULT_WEDGE_AFTER_NS + 1_000_000_000)
    return kernel, command_id


class _AbsentVenue:
    """Every read succeeds; nothing exists at the venue."""

    demo = True
    realm = "demo"

    def __init__(self) -> None:
        self.probe_open_order_calls = 0

    def get_open_orders(self, **params: object):
        if "order_link_id" in params:
            self.probe_open_order_calls += 1
        return []

    def get_order_history(self, **params: object):
        return []

    def get_trade_history(self, **params: object):
        return []

    def get_positions(self, **params: object):
        return []


class _MainnetAbsentVenue(_AbsentVenue):
    demo = False
    realm = "mainnet"


class _LiveVenue(_AbsentVenue):
    """The probe finds a working order under the command id."""

    def __init__(self, command_id: str) -> None:
        super().__init__()
        self._command_id = command_id

    def get_open_orders(self, **params: object):
        if params.get("order_link_id") == self._command_id:
            self.probe_open_order_calls += 1
            return [
                {"orderLinkId": self._command_id, "orderStatus": "New", "orderId": "v1"}
            ]
        return []


def test_demo_reconciler_terminalizes_a_dead_attempted_command(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _wedged_kernel(tmp_path, clock, attempted=True)
    reconciler = BybitAccountReconciler(
        kernel=kernel, client=_AbsentVenue(), instrument_rules=RULES, clock=clock
    )

    report = reconciler.reconcile_once()

    order = kernel.state().orders[command_id]
    assert order.status == "cancelled"
    assert order.rejection_key == RESOLUTION_REJECTION_KEY
    # The wedge cleared inside the same pass, so health is already clean.
    assert report.healthy, report.mismatches


def test_demo_reconciler_terminalizes_a_never_submitted_command(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _wedged_kernel(tmp_path, clock, attempted=False)
    reconciler = BybitAccountReconciler(
        kernel=kernel, client=_AbsentVenue(), instrument_rules=RULES, clock=clock
    )

    report = reconciler.reconcile_once()

    assert kernel.state().orders[command_id].status == "cancelled"
    assert report.healthy, report.mismatches


def test_mainnet_reconciler_terminalizes_on_the_same_evidence_ladder(tmp_path: Path) -> None:
    """Mainnet self-clears too: an operator-only wedge blocked the owner for hours."""

    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _wedged_kernel(tmp_path, clock, attempted=False)
    reconciler = BybitAccountReconciler(
        kernel=kernel, client=_MainnetAbsentVenue(), instrument_rules=RULES, clock=clock
    )
    assert reconciler.auto_resolve_wedges is True

    report = reconciler.reconcile_once()

    assert kernel.state().orders[command_id].status == "cancelled"
    assert report.healthy, report.mismatches


def test_mainnet_still_refuses_a_wedge_the_evidence_does_not_clear(tmp_path: Path) -> None:
    """Automatic resolution changed the realm, not the evidence standard."""

    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _wedged_kernel(tmp_path, clock, attempted=False)
    venue = _LiveVenue(command_id)
    venue.realm = "mainnet"
    venue.demo = False
    reconciler = BybitAccountReconciler(
        kernel=kernel, client=venue, instrument_rules=RULES, clock=clock
    )

    report = reconciler.reconcile_once()

    assert kernel.state().orders[command_id].status == "commanded"
    assert any(mismatch.startswith("wedged_command:") for mismatch in report.mismatches)


def test_a_live_venue_order_is_never_terminalized(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _wedged_kernel(tmp_path, clock, attempted=False)
    venue = _LiveVenue(command_id)
    reconciler = BybitAccountReconciler(
        kernel=kernel, client=venue, instrument_rules=RULES, clock=clock
    )

    report = reconciler.reconcile_once()

    assert venue.probe_open_order_calls == 1
    assert kernel.state().orders[command_id].status == "commanded"
    # Still visible in health until the contradiction clears.
    assert any(mismatch.startswith("wedged_command:") for mismatch in report.mismatches)


def test_a_young_command_is_not_probed(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(
        tmp_path, account_id="wedge-account", clock=clock, id_seed="wedge"
    )
    kernel.submit_targets(
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
        risk_policy=POLICY,
        instrument_rules=RULES,
    )
    venue = _AbsentVenue()
    reconciler = BybitAccountReconciler(
        kernel=kernel, client=venue, instrument_rules=RULES, clock=clock
    )

    reconciler.reconcile_once()

    assert venue.probe_open_order_calls == 0


def test_an_unresolved_wedge_is_probed_at_most_once_per_interval(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _wedged_kernel(tmp_path, clock, attempted=False)
    venue = _LiveVenue(command_id)
    reconciler = BybitAccountReconciler(
        kernel=kernel, client=venue, instrument_rules=RULES, clock=clock
    )

    reconciler.reconcile_once()
    clock.advance_ns(2_000_000_000)  # one ordinary 2s reconcile cadence later
    reconciler.reconcile_once()

    assert venue.probe_open_order_calls == 1
    assert kernel.state().orders[command_id].status == "commanded"


def test_wedge_lines_do_not_block_same_symbol_reductions(tmp_path: Path) -> None:
    """A journal-proven phantom command must not freeze the symbol's exits."""

    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=100)
    kernel, command_id = _wedged_kernel(tmp_path, clock, attempted=False)
    # A live venue order refuses resolution in every realm, so the wedge line
    # survives the automatic pass and the admission split stays observable.
    reconciler = BybitAccountReconciler(
        kernel=kernel, client=_LiveVenue(command_id), instrument_rules=RULES, clock=clock
    )

    reconciler.reconcile_once()

    with pytest.raises(RuntimeError, match="wedged_command"):
        reconciler.require_recent_healthy(max_age_ns=10**12)
    # Per-symbol reduction truth is unaffected by the wedge line.
    reconciler.require_recent_symbols_consistent(["BUSDT"], max_age_ns=10**12)
