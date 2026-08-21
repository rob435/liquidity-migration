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
    WedgeEvidence,
    WedgedCommandResolutionRefused,
    probe_wedged_command,
    resolve_wedged_command,
    terminalize_wedged_command,
)
from liquidity_migration.account.wedged_command_watch import DEFAULT_WEDGE_AFTER_NS

RULES = {"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1)}
POLICY = AccountRiskPolicy(100_000.0, 100_000.0, 100_000.0, 10.0)


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


def test_probe_finds_an_adopted_external_order_by_its_venue_order_id() -> None:
    """An adopted external order was never given an ``orderLinkId``.

    Probing one by client id answered "absent" for an order the venue plainly
    held as Filled, and absent is the classification that needs authorization
    (ACEUSDT, 2026-08-07).
    """

    recorded: list[dict[str, object]] = []

    class _RecordingVenue(_Venue):
        def get_order_history(self, **params: object) -> list[dict[str, object]]:
            recorded.append(dict(params))
            return super().get_order_history(**params)

    venue = _RecordingVenue(
        order_history=[
            {"orderId": "venue-1", "orderStatus": "Filled", "cumExecQty": "9"},
        ]
    )

    evidence = probe_wedged_command(
        client=venue,
        command_id="external-reduction-command",
        symbol="BUSDT",
        venue_order_id="venue-1",
    )

    assert evidence.classification == "terminal"
    assert evidence.venue_order_id == "venue-1"
    assert evidence.observed_filled_qty == 9.0
    # Queried by venue id; the client id the venue never saw is not sent.
    assert recorded == [{"symbol": "BUSDT", "order_id": "venue-1"}]


class _StubOrder:
    def __init__(self, *, filled: float, signed: float, reduce_only: bool) -> None:
        self.command_id = "external-reduction-command"
        self.symbol = "BUSDT"
        self.filled_signed_qty = filled
        self.signed_qty = signed
        self.reduce_only = reduce_only


class _StubWedge:
    kind = "stalled_working_order"
    age_ns = 10_000_000_000
    blocks_exit = False


class _RecordingKernel:
    def __init__(self) -> None:
        self.recorded: list[dict[str, object]] = []

    def record_order_status(self, **kwargs: object) -> None:
        self.recorded.append(dict(kwargs))


def test_a_flat_book_terminalizes_a_reduction_the_venue_filled_larger() -> None:
    """Venue quantity past what the book reduced is foreign, not a lost fill."""

    kernel = _RecordingKernel()
    evidence = WedgeEvidence(
        command_id="external-reduction-command",
        symbol="BUSDT",
        classification="terminal",
        venue_order_status="filled",
        venue_order_id="venue-1",
        observed_filled_qty=9.0,
    )

    terminalize_wedged_command(
        kernel=kernel,
        order=_StubOrder(filled=-2.0, signed=-2.0, reduce_only=True),
        wedge=_StubWedge(),
        evidence=evidence,
        now_ns=1_000,
        resolved_by="test",
        owned_position_qty=0.0,
    )

    assert len(kernel.recorded) == 1
    assert kernel.recorded[0]["status"] == "partially_filled_cancelled"


def test_a_book_still_holding_the_position_refuses_the_unreconstructed_fills() -> None:
    """The evidence standard is unchanged while a reduction can still be lost."""

    evidence = WedgeEvidence(
        command_id="external-reduction-command",
        symbol="BUSDT",
        classification="terminal",
        venue_order_id="venue-1",
        observed_filled_qty=9.0,
    )

    with pytest.raises(WedgedCommandResolutionRefused, match="reconciliation"):
        terminalize_wedged_command(
            kernel=_RecordingKernel(),
            order=_StubOrder(filled=-2.0, signed=-5.0, reduce_only=True),
            wedge=_StubWedge(),
            evidence=evidence,
            now_ns=1_000,
            resolved_by="test",
            owned_position_qty=3.0,
        )


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


def test_operator_and_reason_are_optional_receipts(tmp_path: Path) -> None:
    """Resolution is authorized by evidence, not by typing intent strings."""

    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)

    evidence = resolve_wedged_command(
        kernel=kernel,
        client=_Venue(),
        command_id=wedged_id,
        authorize_absent=True,
    )

    assert evidence.classification == "absent"
    journaled = [
        event
        for event in read_account_journal(tmp_path)
        if str(event.payload.get("command_id") or "") == wedged_id
        and (event.payload.get("metadata") or {}).get("wedged_command_resolution")
    ]
    metadata = journaled[0].payload["metadata"]
    assert metadata["resolved_by"] == "cli"
    assert "operator" not in metadata
    assert "reason" not in metadata


def test_an_attempted_command_still_needs_absent_authorization(tmp_path: Path) -> None:
    """Absence after a real submission attempt may be venue visibility lag."""

    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)

    with pytest.raises(WedgedCommandResolutionRefused, match="absent-order authorization"):
        resolve_wedged_command(kernel=kernel, client=_Venue(), command_id=wedged_id)


def test_a_never_submitted_command_needs_no_absent_authorization(tmp_path: Path) -> None:
    """The journal proves zero dispatch attempts; venue absence merely confirms it."""

    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="wedge", clock=clock, id_seed="never")
    parked = kernel.submit_targets(
        batch_id="never-dispatched",
        market_inputs=(_market(),),
        targets=(_target(5.0, decision="d-never"),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 950),
        risk_policy=POLICY,
        instrument_rules=RULES,
    )
    never_id = parked.commands[0].command_id
    assert kernel.state().orders[never_id].submission_attempts == 0
    clock.advance_ns(DEFAULT_WEDGE_AFTER_NS + 1_000_000_000)

    evidence = resolve_wedged_command(kernel=kernel, client=_Venue(), command_id=never_id)

    assert evidence.classification == "absent"
    assert kernel.state().orders[never_id].status == "cancelled"


def test_terminal_answer_outranks_a_failed_trade_history_read(tmp_path: Path) -> None:
    """A rate-limited fills read must not veto an unambiguous Cancelled row."""

    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)
    venue = _Venue(
        order_history=[
            {"orderLinkId": wedged_id, "orderStatus": "Cancelled", "orderId": "v7", "cumExecQty": "0"}
        ],
        fail="trade_history",
    )

    evidence = resolve_wedged_command(
        kernel=kernel, client=venue, command_id=wedged_id
    )

    assert evidence.classification == "terminal"
    assert kernel.state().orders[wedged_id].status == "cancelled"


def test_terminal_row_fill_quantity_still_blocks_resolution(tmp_path: Path) -> None:
    """cumExecQty on the terminal row itself proves unreconstructed fills even
    when the trade-history read failed."""

    kernel, _clock, wedged_id = _wedged_kernel(tmp_path)
    venue = _Venue(
        order_history=[
            {
                "orderLinkId": wedged_id,
                "orderStatus": "PartiallyFilledCanceled",
                "orderId": "v8",
                "cumExecQty": "2",
            }
        ],
        fail="trade_history",
    )

    with pytest.raises(WedgedCommandResolutionRefused, match="let reconciliation reduce"):
        resolve_wedged_command(kernel=kernel, client=venue, command_id=wedged_id)


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


def _stalled_exit_kernel(tmp_path: Path) -> tuple[AccountExecutionKernel, VirtualClock, str]:
    """Open 5 units, then leave a reduce-only exit ``partially_filled`` with
    its venue side gone — the BANKUSDT 2026-08-01 stranding."""

    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="stall", clock=clock, id_seed="stall")
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
    closing = kernel.submit_targets(
        batch_id="close",
        market_inputs=(_market(key="book-3"),),
        targets=(_target(0.0, decision="d-close"),),
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet-3", 1_400_000_000),
        risk_policy=POLICY,
        instrument_rules=RULES,
    )
    exit_id = closing.commands[0].command_id
    kernel.record_submission_attempt(command_id=exit_id, adapter_name="bybit_demo")
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=exit_id,
                exchange_ts_ns=1_500_000_000,
                local_receive_ts_ns=1_510_000_000,
                accepted=True,
                venue_order_id="venue-close",
            ),
            ExecutionObservation(
                observation_type="fill",
                command_id=exit_id,
                exchange_ts_ns=1_520_000_000,
                local_receive_ts_ns=1_530_000_000,
                venue_order_id="venue-close",
                execution_id="exec-close-1",
                signed_qty=-2.0,
                price=10.0,
                fee_usdt=0.01,
            ),
        )
    )
    assert kernel.state().orders[exit_id].status == "partially_filled"
    clock.advance_ns(DEFAULT_WEDGE_AFTER_NS + 1_000_000_000)
    return kernel, clock, exit_id


def test_a_stalled_partial_fill_resolves_on_authorized_absent_evidence(tmp_path: Path) -> None:
    kernel, _clock, exit_id = _stalled_exit_kernel(tmp_path)

    evidence = resolve_wedged_command(
        kernel=kernel,
        client=_Venue(),
        command_id=exit_id,
        operator="owner",
        reason="venue shows no open order and the position is flat",
        authorize_absent=True,
    )

    assert evidence.classification == "absent"
    resolved = kernel.state().orders[exit_id]
    assert resolved.status == "partially_filled_cancelled"
    assert exit_id not in kernel.state().working_order_ids
    last = read_account_journal(tmp_path)[-1]
    assert last.payload["rejection_key"] == RESOLUTION_REJECTION_KEY
    assert last.payload["metadata"]["wedge_kind"] == "stalled_working_order"
    assert last.payload["cumulative_filled_qty"] == pytest.approx(2.0)


def test_a_stalled_working_order_still_live_at_the_venue_is_refused(tmp_path: Path) -> None:
    kernel, _clock, exit_id = _stalled_exit_kernel(tmp_path)
    venue = _Venue(
        open_orders=[{"orderLinkId": exit_id, "orderStatus": "PartiallyFilled", "orderId": "v9"}]
    )

    with pytest.raises(WedgedCommandResolutionRefused, match="still holds a working order"):
        resolve_wedged_command(
            kernel=kernel,
            client=venue,
            command_id=exit_id,
            operator="owner",
            reason="manual check",
            authorize_absent=True,
        )
    assert kernel.state().orders[exit_id].status == "partially_filled"


def test_a_terminal_order_has_no_wedge_to_resolve(tmp_path: Path) -> None:
    kernel, _clock, exit_id = _stalled_exit_kernel(tmp_path)
    resolve_wedged_command(
        kernel=kernel,
        client=_Venue(),
        command_id=exit_id,
        operator="owner",
        reason="venue shows no open order and the position is flat",
        authorize_absent=True,
    )

    with pytest.raises(WedgedCommandResolutionRefused, match="not a working order"):
        resolve_wedged_command(
            kernel=kernel,
            client=_Venue(),
            command_id=exit_id,
            operator="owner",
            reason="second attempt",
            authorize_absent=True,
        )
