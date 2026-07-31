"""B15b — the exit from a wedged ``commanded`` order, and the exits it unblocks."""

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
    read_account_journal,
)
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.execution_adapters import ExecutionObservation, KernelExecutionDriver
from liquidity_migration.venue.wedged_command_resolution import (
    RESOLUTION_REJECTION_KEY,
    WedgedCommandResolutionRefused,
    probe_wedged_command,
    resolve_wedged_command,
)
from liquidity_migration.account.wedged_command_watch import DEFAULT_WEDGE_AFTER_NS

RULES = {"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1)}
POLICY = AccountRiskPolicy(100_000.0, 100_000.0, 100_000.0, 100_000.0, 10.0)


def _market(price: float = 10.0, *, key: str = "book-1") -> MarketInputRef:
    return MarketInputRef(key, "BUSDT", 900, 1_000, price)


def _target(signed_qty: float, *, decision: str) -> DesiredTarget:
    return DesiredTarget(
        decision_key=decision,
        target_key="long/strategy/trade/BUSDT",
        sleeve="long",
        strategy_id="strategy",
        component_id="trade",
        symbol="BUSDT",
        signed_qty=signed_qty,
        reference_price=10.0,
        leverage=1.0,
    )


def _wedged_kernel(tmp_path: Path) -> tuple[AccountExecutionKernel, VirtualClock, str]:
    """Open 5 units, then leave a second entry command wedged in ``commanded``."""

    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="wedge", clock=clock, id_seed="wedge")
    opened = kernel.submit_targets(
        batch_id="open",
        market_inputs=(_market(),),
        targets=(_target(5.0, decision="d-open"),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 950),
        risk_policy=POLICY,
        instrument_rules=RULES,
    )
    entry = opened.commands[0]
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=entry.command_id,
                exchange_ts_ns=1_100_000_000,
                local_receive_ts_ns=1_110_000_000,
                accepted=True,
                venue_order_id="venue-open",
            ),
            ExecutionObservation(
                observation_type="fill",
                command_id=entry.command_id,
                exchange_ts_ns=1_120_000_000,
                local_receive_ts_ns=1_130_000_000,
                venue_order_id="venue-open",
                execution_id="exec-open",
                signed_qty=5.0,
                price=10.0,
                fee_usdt=0.01,
            ),
        )
    )
    # A scale-in that is journaled, attempted, and then loses its answer.
    scaled = kernel.submit_targets(
        batch_id="scale-in",
        market_inputs=(_market(key="book-2"),),
        targets=(_target(8.0, decision="d-scale"),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet-2", 1_200_000_000),
        risk_policy=POLICY,
        instrument_rules=RULES,
    )
    wedged_id = scaled.commands[0].command_id
    kernel.record_submission_attempt(command_id=wedged_id, adapter_name="bybit_demo")
    assert kernel.state().orders[wedged_id].status == "commanded"
    clock.advance_ns(DEFAULT_WEDGE_AFTER_NS + 1_000_000_000)
    return kernel, clock, wedged_id


class _Venue:
    """Read-only probe surface with independently controllable answers."""

    def __init__(
        self,
        *,
        open_orders: list[dict[str, object]] | None = None,
        order_history: list[dict[str, object]] | None = None,
        trade_history: list[dict[str, object]] | None = None,
        fail: str = "",
    ) -> None:
        self._open_orders = open_orders or []
        self._order_history = order_history or []
        self._trade_history = trade_history or []
        self._fail = fail

    def _maybe_fail(self, label: str) -> None:
        if self._fail == label:
            raise TimeoutError("venue query timed out")

    def get_open_orders(self, **_params: object) -> list[dict[str, object]]:
        self._maybe_fail("open_orders")
        return list(self._open_orders)

    def get_order_history(self, **_params: object) -> list[dict[str, object]]:
        self._maybe_fail("order_history")
        return list(self._order_history)

    def get_trade_history(self, **_params: object) -> list[dict[str, object]]:
        self._maybe_fail("trade_history")
        return list(self._trade_history)


def test_probe_classifies_a_live_order_as_unresolvable(tmp_path: Path) -> None:
    _kernel, _clock, wedged_id = _wedged_kernel(tmp_path)
    venue = _Venue(open_orders=[{"orderLinkId": wedged_id, "orderStatus": "New", "orderId": "v1"}])

    evidence = probe_wedged_command(client=venue, command_id=wedged_id, symbol="BUSDT")

    assert evidence.classification == "live"
    assert evidence.venue_order_id == "v1"


def test_probe_classifies_a_failed_query_as_unreadable(tmp_path: Path) -> None:
    _kernel, _clock, wedged_id = _wedged_kernel(tmp_path)
    venue = _Venue(fail="order_history")

    evidence = probe_wedged_command(client=venue, command_id=wedged_id, symbol="BUSDT")

    assert evidence.classification == "unreadable"
    assert any("TimeoutError" in error for error in evidence.query_errors)


def test_resolution_refuses_while_the_venue_still_holds_the_order(tmp_path: Path) -> None:
    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)
    venue = _Venue(open_orders=[{"orderLinkId": wedged_id, "orderStatus": "New"}])

    with pytest.raises(WedgedCommandResolutionRefused, match="still holds a working order"):
        resolve_wedged_command(
            kernel=kernel,
            client=venue,
            command_id=wedged_id,
            operator="owner",
            reason="manual check",
            authorize_absent=True,
        )
    assert kernel.state().orders[wedged_id].status == "commanded"


def test_resolution_refuses_when_venue_truth_is_unreadable(tmp_path: Path) -> None:
    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)

    with pytest.raises(WedgedCommandResolutionRefused, match="not an absent order"):
        resolve_wedged_command(
            kernel=kernel,
            client=_Venue(fail="trade_history"),
            command_id=wedged_id,
            operator="owner",
            reason="manual check",
            authorize_absent=True,
        )


def test_resolution_refuses_an_absent_order_without_explicit_authorization(
    tmp_path: Path,
) -> None:
    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)

    with pytest.raises(WedgedCommandResolutionRefused, match="strong evidence, not proof"):
        resolve_wedged_command(
            kernel=kernel,
            client=_Venue(),
            command_id=wedged_id,
            operator="owner",
            reason="checked the account by hand",
        )


def test_resolution_refuses_while_fills_are_unreconstructed(tmp_path: Path) -> None:
    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)
    venue = _Venue(
        order_history=[{"orderLinkId": wedged_id, "orderStatus": "Filled"}],
        trade_history=[{"orderLinkId": wedged_id, "execId": "e1", "execQty": "3"}],
    )

    with pytest.raises(WedgedCommandResolutionRefused, match="let reconciliation reduce"):
        resolve_wedged_command(
            kernel=kernel,
            client=venue,
            command_id=wedged_id,
            operator="owner",
            reason="venue says filled",
        )


def test_resolution_refuses_a_command_that_may_still_be_in_flight(tmp_path: Path) -> None:
    kernel, clock, wedged_id = _wedged_kernel(tmp_path)

    with pytest.raises(WedgedCommandResolutionRefused, match="younger than the wedge bound"):
        resolve_wedged_command(
            kernel=kernel,
            client=_Venue(),
            command_id=wedged_id,
            operator="owner",
            reason="impatient",
            authorize_absent=True,
            now_ns=clock.wall_time_ns(),
            wedge_after_ns=10 * DEFAULT_WEDGE_AFTER_NS,
        )


def test_resolution_requires_a_named_operator_and_a_reason(tmp_path: Path) -> None:
    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)
    for operator, reason, message in (
        ("", "why", "named operator"),
        ("owner", "  ", "explicit reason"),
    ):
        with pytest.raises(WedgedCommandResolutionRefused, match=message):
            resolve_wedged_command(
                kernel=kernel,
                client=_Venue(),
                command_id=wedged_id,
                operator=operator,
                reason=reason,
                authorize_absent=True,
            )


def test_venue_terminal_status_resolves_without_absent_authorization(tmp_path: Path) -> None:
    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)
    venue = _Venue(order_history=[{"orderLinkId": wedged_id, "orderStatus": "Cancelled", "orderId": "v9"}])

    evidence = resolve_wedged_command(
        kernel=kernel,
        client=venue,
        command_id=wedged_id,
        operator="owner",
        reason="venue reports cancelled",
    )

    assert evidence.classification == "terminal"
    order = kernel.state().orders[wedged_id]
    assert order.status == "cancelled"
    assert order.rejection_key == RESOLUTION_REJECTION_KEY
    assert order.terminal_status_recorded
    # The symbol is no longer frozen.
    assert "BUSDT" not in kernel.state().working_symbols()


def test_authorized_absent_order_resolves_and_records_who_said_so(tmp_path: Path) -> None:
    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)

    evidence = resolve_wedged_command(
        kernel=kernel,
        client=_Venue(),
        command_id=wedged_id,
        operator="owner@vps",
        reason="checked the Bybit UI; no such order",
        authorize_absent=True,
    )

    assert evidence.classification == "absent"
    assert kernel.state().orders[wedged_id].status == "cancelled"
    journaled = [
        event
        for event in read_account_journal(tmp_path)
        if str(event.payload.get("command_id") or "") == wedged_id
        and (event.payload.get("metadata") or {}).get("wedged_command_resolution")
    ]
    assert len(journaled) == 1
    metadata = journaled[0].payload["metadata"]
    assert metadata["operator"] == "owner@vps"
    assert metadata["reason"] == "checked the Bybit UI; no such order"
    assert metadata["authorized_absent_order"] is True
    assert metadata["venue_evidence"]["classification"] == "absent"


def test_a_wedged_symbol_can_still_be_exited(tmp_path: Path) -> None:
    """The point of B15b: the position is never stranded behind the wedge."""

    kernel, clock, wedged_id = _wedged_kernel(tmp_path)
    assert kernel.state().positions["BUSDT"].signed_qty == pytest.approx(5.0)

    result = kernel.submit_targets(
        batch_id="exit",
        market_inputs=(_market(key="book-exit"),),
        targets=(_target(0.0, decision="d-exit"),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet-3", clock.wall_time_ns()),
        risk_policy=POLICY,
        instrument_rules=RULES,
    )

    assert result.accepted, result.rejections
    assert len(result.commands) == 1
    command = result.commands[0]
    assert command.reduce_only
    assert command.side == "Sell"
    # Sized against the reconstructed position ALONE (5), not against the
    # position plus the wedged scale-in (8) — asking to sell 8 is the 110017
    # reject loop the original freeze existed to avoid.
    assert command.qty == pytest.approx(5.0)
    # The wedged command is untouched: nothing was resent or terminalized.
    assert kernel.state().orders[wedged_id].status == "commanded"


def test_a_wedged_symbol_still_refuses_new_exposure(tmp_path: Path) -> None:
    kernel, clock, wedged_id = _wedged_kernel(tmp_path)

    result = kernel.submit_targets(
        batch_id="more",
        market_inputs=(_market(key="book-more"),),
        targets=(_target(9.0, decision="d-more"),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet-4", clock.wall_time_ns()),
        risk_policy=POLICY,
        instrument_rules=RULES,
    )

    assert result.commands == ()
    assert kernel.state().orders[wedged_id].status == "commanded"


def test_a_live_working_order_still_freezes_the_symbol(tmp_path: Path) -> None:
    """Only a wedge unblocks exits; a genuinely in-flight order must not."""

    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="live", clock=clock, id_seed="live")
    opened = kernel.submit_targets(
        batch_id="open",
        market_inputs=(_market(),),
        targets=(_target(5.0, decision="d-open"),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 950),
        risk_policy=POLICY,
        instrument_rules=RULES,
    )
    entry = opened.commands[0]
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=entry.command_id,
                exchange_ts_ns=1_100_000_000,
                local_receive_ts_ns=1_110_000_000,
                accepted=True,
                venue_order_id="venue-open",
            ),
            ExecutionObservation(
                observation_type="fill",
                command_id=entry.command_id,
                exchange_ts_ns=1_120_000_000,
                local_receive_ts_ns=1_130_000_000,
                venue_order_id="venue-open",
                execution_id="exec-open",
                signed_qty=5.0,
                price=10.0,
                fee_usdt=0.01,
            ),
        )
    )
    kernel.submit_targets(
        batch_id="scale-in",
        market_inputs=(_market(key="book-2"),),
        targets=(_target(8.0, decision="d-scale"),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet-2", 1_200_000_000),
        risk_policy=POLICY,
        instrument_rules=RULES,
    )
    # Seconds old, not minutes: still plausibly in flight.
    clock.advance_ns(2_000_000_000)

    result = kernel.submit_targets(
        batch_id="exit",
        market_inputs=(_market(key="book-exit"),),
        targets=(_target(0.0, decision="d-exit"),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet-3", clock.wall_time_ns()),
        risk_policy=POLICY,
        instrument_rules=RULES,
    )

    assert result.commands == ()
