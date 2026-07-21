from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import liquidity_migration.account_notifications as account_notifications_module
from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.account_notifications import AccountNotificationEngine, HOUR_NS
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.execution_adapters import ExecutionObservation, KernelExecutionDriver


def _setup_open(root: Path):
    clock = VirtualClock(current_wall_ns=10 * HOUR_NS, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(root, account_id="notify", clock=clock, id_seed="notify")
    market = MarketInputRef("book-open", "BUSDT", 1, 2, 10.0)
    rules = {"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1)}
    policy = AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0)
    snapshot = AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 3)
    opened = kernel.submit_targets(
        batch_id="open",
        market_inputs=[market],
        targets=[
            DesiredTarget(
                decision_key="open-d",
                target_key="long/strategy/trade/BUSDT",
                sleeve="long",
                strategy_id="strategy",
                component_id="trade",
                symbol="BUSDT",
                signed_qty=2.0,
                reference_price=10.0,
                leverage=10.0,
                reason="entry",
                metadata={"stop_price": 9.0},
            )
        ],
        risk_snapshot=snapshot,
        risk_policy=policy,
        instrument_rules=rules,
    )
    command = opened.commands[0]
    driver = KernelExecutionDriver(kernel)
    driver.ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=command.command_id,
                exchange_ts_ns=4,
                local_receive_ts_ns=5,
                accepted=True,
                venue_order_id="entry-order",
            ),
            ExecutionObservation(
                observation_type="fill",
                command_id=command.command_id,
                exchange_ts_ns=6,
                local_receive_ts_ns=7,
                venue_order_id="entry-order",
                execution_id="entry-fill",
                signed_qty=2.0,
                price=10.0,
                fee_usdt=0.01,
            ),
        )
    )
    kernel.record_protection(
        protection_key="native:BUSDT",
        symbol="BUSDT",
        status="active",
        stop_price=9.0,
        take_profit_price=None,
        exchange_ts_ns=0,
        local_receive_ts_ns=8,
        metadata={"native_exchange": True, "symbol": "BUSDT"},
    )
    return kernel, clock, market, rules, policy, snapshot, driver


def _setup_risk_notifications(root: Path):
    clock = VirtualClock(current_wall_ns=20 * HOUR_NS, current_monotonic_ns=200)
    kernel = AccountExecutionKernel(root, account_id="notify-risk", clock=clock, id_seed="risk")
    snapshot = AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 3)
    loose_policy = AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=1_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=1_000.0,
        max_leverage=10.0,
    )
    rules = {
        "BUSDT": InstrumentRules(
            "BUSDT",
            qty_step=0.1,
            min_qty=0.1,
            min_notional=0.1,
            tick_size=0.1,
        )
    }
    kernel.submit_targets(
        batch_id="notification-bootstrap",
        market_inputs=[MarketInputRef("book-bootstrap", "BUSDT", 1, 2, 10.0)],
        targets=[
            DesiredTarget(
                decision_key="continuous-target/notify/1/reconcile/bootstrap",
                target_key="continuous/notify/bootstrap/BUSDT",
                sleeve="continuous",
                strategy_id="notify",
                component_id="bootstrap",
                symbol="BUSDT",
                signed_qty=0.0,
                reference_price=10.0,
                leverage=10.0,
                reason="bootstrap",
            )
        ],
        risk_snapshot=snapshot,
        risk_policy=loose_policy,
        instrument_rules=rules,
    )
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=root.parent / "notify-risk-state.json",
        clock=clock,
    )
    notifier.commit(notifier.prepare(midpoint_by_symbol={}, health="healthy"))
    return kernel, clock, notifier, snapshot, loose_policy


def _submit_entry_risk_decision(
    kernel: AccountExecutionKernel,
    *,
    snapshot: AccountRiskSnapshot,
    loose_policy: AccountRiskPolicy,
    batch_id: str,
    components: tuple[str, ...] = ("trade-a",),
    rejection: str | None = "below_min_notional",
    explicit_attempt_keys: bool = True,
    signal_valid_until_ms: int | None = None,
):
    policy = loose_policy
    min_notional = 0.1
    if rejection == "below_min_notional":
        min_notional = 5.0
    elif rejection == "component_gross_limit":
        policy = AccountRiskPolicy(
            max_component_gross_notional_usdt=0.5,
            max_account_gross_notional_usdt=1_000.0,
            max_symbol_notional_usdt=1_000.0,
            max_initial_margin_usdt=1_000.0,
            max_leverage=10.0,
        )
    elif rejection is not None:
        raise ValueError(f"unsupported test rejection: {rejection}")
    targets = []
    for component in components:
        metadata = {"entry_attempt_key": f"attempt:{component}"} if explicit_attempt_keys else {}
        if signal_valid_until_ms is not None:
            metadata.update(
                {
                    "signal_ts_ms": signal_valid_until_ms - 3_600_000,
                    "signal_valid_until_ms": signal_valid_until_ms,
                }
            )
        targets.append(
            DesiredTarget(
                decision_key=(f"continuous-target/notify/{batch_id}/entry/{component}"),
                target_key=f"continuous/notify/{component}/BUSDT",
                sleeve="continuous",
                strategy_id="notify",
                component_id=component,
                symbol="BUSDT",
                signed_qty=0.1,
                reference_price=10.0,
                leverage=10.0,
                reason="signal",
                metadata=metadata,
            )
        )
    return kernel.submit_targets(
        batch_id=batch_id,
        market_inputs=[MarketInputRef(f"book-{batch_id}", "BUSDT", 10, 11, 10.0)],
        targets=targets,
        risk_snapshot=snapshot,
        risk_policy=policy,
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT",
                qty_step=0.1,
                min_qty=0.1,
                min_notional=min_notional,
                tick_size=0.1,
            )
        },
    )


def test_notification_commit_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notifications" / "state.json",
        clock=clock,
    )
    observed_modes: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        observed_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(account_notifications_module.os, "fsync", recording_fsync)

    notifier.commit(notifier.prepare(midpoint_by_symbol={"BUSDT": 10.0}, health="healthy"))

    assert sum(stat.S_ISREG(mode) for mode in observed_modes) >= 1
    assert sum(stat.S_ISDIR(mode) for mode in observed_modes) >= 1


def test_notification_commit_removes_temporary_file_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notifications" / "state.json",
        clock=clock,
    )

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(account_notifications_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        notifier.commit(notifier.prepare(midpoint_by_symbol={"BUSDT": 10.0}, health="healthy"))

    assert list(notifier.state_path.parent.glob(".*.tmp")) == []
    assert not notifier.state_path.exists()


def test_first_run_silently_recovers_unresolved_entry_risk_state(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=20 * HOUR_NS, current_monotonic_ns=200)
    kernel = AccountExecutionKernel(
        tmp_path / "account",
        account_id="notify-risk",
        clock=clock,
        id_seed="risk",
    )
    snapshot = AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 3)
    policy = AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0)
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="rejected-before-notifier-start",
    )
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )

    first = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert first.event_messages == ()
    assert "Entry blocked by account risk" not in first.message
    assert "Entry risk: 1 unresolved attempt(s)" in first.message
    assert "below min notional" in first.message
    assert "0 rejected evaluation" not in first.message


def test_hourly_summary_loss_alert_and_confirmed_close_are_low_noise(tmp_path: Path) -> None:
    kernel, clock, market, rules, policy, snapshot, driver = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )

    first = notifier.prepare(midpoint_by_symbol={"BUSDT": 10.0}, health="healthy")
    assert first.hourly_included
    assert "Bybit demo · account update" in first.message
    assert "BUSDT long 2" in first.message
    assert "SL $9" in first.message
    assert "Opened" not in first.message  # no replay storm on first startup
    # Until delivery is acknowledged the exact hourly update remains pending.
    assert notifier.prepare(midpoint_by_symbol={"BUSDT": 10.0}, health="healthy").message == first.message
    notifier.commit(first)

    loss = notifier.prepare(midpoint_by_symbol={"BUSDT": 9.4}, health="healthy")
    assert not loss.hourly_included
    assert "is losing -6.0%" in loss.message
    assert "estimated -$1.20" in loss.message
    assert "L2 midpoint $9.4" in loss.message
    assert "mark" not in loss.message.lower()
    notifier.commit(loss)
    assert notifier.prepare(midpoint_by_symbol={"BUSDT": 9.3}, health="healthy").message == ""

    closing = kernel.submit_targets(
        batch_id="close",
        market_inputs=[MarketInputRef("book-close", "BUSDT", 9, 10, 9.2)],
        targets=[
            DesiredTarget(
                decision_key="close-d",
                target_key="long/strategy/trade/BUSDT",
                sleeve="long",
                strategy_id="strategy",
                component_id="trade",
                symbol="BUSDT",
                signed_qty=0.0,
                reference_price=9.2,
                leverage=10.0,
                reason="time_stop",
            )
        ],
        risk_snapshot=snapshot,
        risk_policy=policy,
        instrument_rules=rules,
    )
    close_command = closing.commands[0]
    driver.ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=close_command.command_id,
                exchange_ts_ns=11,
                local_receive_ts_ns=12,
                accepted=True,
                venue_order_id="close-order",
            ),
            ExecutionObservation(
                observation_type="fill",
                command_id=close_command.command_id,
                exchange_ts_ns=13,
                local_receive_ts_ns=14,
                venue_order_id="close-order",
                execution_id="close-fill",
                signed_qty=-2.0,
                price=9.2,
                fee_usdt=0.02,
            ),
        )
    )

    closed = notifier.prepare(midpoint_by_symbol={}, health="healthy")
    assert "✅ Closed BUSDT · time stop" in closed.message
    assert "P&L -$1.63" in closed.message
    assert "funding journaled separately" in closed.message
    assert "venue closed-PnL not cross-checked online" in closed.message
    assert "fees unresolved" in closed.message
    assert "component P&L not allocated (account-netted)" in closed.message
    assert "awaiting" not in closed.message.lower()
    notifier.commit(closed)

    clock.advance_ns(HOUR_NS)
    flat = notifier.prepare(midpoint_by_symbol={}, health="healthy")
    assert "Flat · no open positions" in flat.message
    assert "realized -$1.63" in flat.message
    assert "funding journaled separately" in flat.message
    assert "venue closed-PnL not cross-checked online" in flat.message


def test_fill_pnl_without_reconciliation_status_stays_conservatively_provisional(
    tmp_path: Path,
) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )
    notifier.commit(notifier.prepare(midpoint_by_symbol={"BUSDT": 10.0}, health="healthy"))
    kernel.record_close(
        close_key="provisional-reduction",
        symbol="BUSDT",
        reason="take_profit",
        venue_flat=False,
        exchange_ts_ns=9,
        local_receive_ts_ns=10,
    )
    kernel.record_pnl(
        pnl_key="provisional-fill-pnl",
        close_key="provisional-reduction",
        symbol="BUSDT",
        gross_pnl_usdt=1.0,
        fee_usdt=0.0,
        funding_usdt=0.0,
        net_pnl_usdt=1.0,
        exchange_ts_ns=11,
        local_receive_ts_ns=12,
        source="fill_reconstructed_provisional_funding",
        metadata={
            "funding_status": "pending_venue_reconciliation",
            # Malformed metadata must not render one component per
            # character or accidentally imply a supported attribution.
            "component_ids": "bad-component",
        },
    )

    update = notifier.prepare(midpoint_by_symbol={"BUSDT": 10.0}, health="healthy")

    assert "✅ Reduced BUSDT · take profit" in update.message
    assert "funding journaled separately" in update.message
    assert "venue closed-PnL not cross-checked online" in update.message
    assert "fees unresolved" in update.message
    assert "component b, a, d" not in update.message


def test_same_symbol_component_exit_reports_reduction_before_later_close(
    tmp_path: Path,
) -> None:
    kernel, clock, _, rules, policy, snapshot, driver = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )
    notifier.commit(notifier.prepare(midpoint_by_symbol={"BUSDT": 10.0}, health="healthy"))

    def target_and_fill(
        *,
        batch_id: str,
        target_key: str,
        component_id: str,
        target_qty: float,
        price: float,
        reason: str,
        fee: float,
    ) -> None:
        result = kernel.submit_targets(
            batch_id=batch_id,
            market_inputs=[MarketInputRef(f"book-{batch_id}", "BUSDT", 20, 21, price)],
            targets=[
                DesiredTarget(
                    decision_key=f"decision-{batch_id}",
                    target_key=target_key,
                    sleeve="long",
                    strategy_id="strategy",
                    component_id=component_id,
                    symbol="BUSDT",
                    signed_qty=target_qty,
                    reference_price=price,
                    leverage=10.0,
                    reason=reason,
                )
            ],
            risk_snapshot=snapshot,
            risk_policy=policy,
            instrument_rules=rules,
        )
        command = result.commands[0]
        driver.ingest(
            (
                ExecutionObservation(
                    observation_type="ack",
                    command_id=command.command_id,
                    exchange_ts_ns=22,
                    local_receive_ts_ns=23,
                    accepted=True,
                    venue_order_id=f"venue-{batch_id}",
                ),
                ExecutionObservation(
                    observation_type="fill",
                    command_id=command.command_id,
                    exchange_ts_ns=24,
                    local_receive_ts_ns=25,
                    venue_order_id=f"venue-{batch_id}",
                    execution_id=f"execution-{batch_id}",
                    signed_qty=command.signed_qty,
                    price=price,
                    fee_usdt=fee,
                    metadata={
                        "fee_observed": True,
                        "fee_status": "observed_execution_fee",
                        "fee_source": "test_execution_fee",
                        "source": "test_venue_execution",
                    },
                ),
            )
        )

    target_and_fill(
        batch_id="open-b",
        target_key="long/strategy/component-b/BUSDT",
        component_id="component-b",
        target_qty=1.0,
        price=20.0,
        reason="entry",
        fee=0.01,
    )
    notifier.commit(notifier.prepare(midpoint_by_symbol={"BUSDT": 20.0}, health="healthy"))

    target_and_fill(
        batch_id="close-first-component",
        target_key="long/strategy/trade/BUSDT",
        component_id="trade",
        target_qty=0.0,
        price=12.0,
        reason="take_profit",
        fee=0.02,
    )
    first_exit = notifier.prepare(midpoint_by_symbol={"BUSDT": 12.0}, health="healthy")
    assert "✅ Reduced BUSDT · take profit · component trade" in first_exit.message
    assert "component P&L not allocated (account-netted)" in first_exit.message
    notifier.commit(first_exit)

    target_and_fill(
        batch_id="close-second-component",
        target_key="long/strategy/component-b/BUSDT",
        component_id="component-b",
        target_qty=0.0,
        price=18.0,
        reason="time_stop",
        fee=0.01,
    )
    second_exit = notifier.prepare(midpoint_by_symbol={}, health="healthy")
    assert "✅ Closed BUSDT · time stop · component component-b" in second_exit.message
    assert "take profit" not in second_exit.message
    assert len(kernel.state().pnl) == 2


def test_venue_flat_mismatch_suppresses_phantom_position_loss_alerts(tmp_path: Path) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )

    mismatch = notifier.prepare(
        midpoint_by_symbol={"BUSDT": 8.0},
        health="BLOCKED · BUSDT venue=0 reconstructed=2",
        venue_positions={},
        position_truth_healthy=False,
    )

    assert mismatch.hourly_included
    assert "Position truth mismatch" in mismatch.message
    assert "Venue: flat" in mismatch.message
    assert "Local reconstruction: BUSDT long 2" in mismatch.message
    assert "exposure/estimated uPnL suppressed until reconciled" in mismatch.message
    assert "is losing" not in mismatch.message


def test_stale_matching_position_truth_is_not_called_a_mismatch(tmp_path: Path) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )

    stale = notifier.prepare(
        midpoint_by_symbol={"BUSDT": 10.0},
        health="BLOCKED · account reconciliation is stale",
        venue_positions={"BUSDT": 2.0},
        position_truth_healthy=False,
        position_truth_status="stale",
    )

    assert "Position truth stale" in stale.message
    assert "Position truth mismatch" not in stale.message
    assert "Venue: BUSDT long 2" in stale.message
    assert "Local reconstruction: BUSDT long 2" in stale.message


def test_position_mismatch_never_renders_green_close_event(tmp_path: Path) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )
    notifier.commit(
        notifier.prepare(
            midpoint_by_symbol={"BUSDT": 10.0},
            health="healthy",
            venue_positions={"BUSDT": 2.0},
        )
    )
    kernel.record_close(
        close_key="local-only-close",
        symbol="BUSDT",
        reason="take_profit",
        venue_flat=False,
        exchange_ts_ns=10,
        local_receive_ts_ns=11,
        metadata={
            "reconstructed_flat": True,
            "venue_position_status": "pending_reconciliation",
        },
    )
    kernel.record_pnl(
        pnl_key="local-only-pnl",
        close_key="local-only-close",
        symbol="BUSDT",
        gross_pnl_usdt=1.0,
        fee_usdt=0.01,
        funding_usdt=0.0,
        net_pnl_usdt=0.99,
        exchange_ts_ns=10,
        local_receive_ts_ns=11,
        source="fill_reconstructed_provisional_funding",
    )

    update = notifier.prepare(
        midpoint_by_symbol={},
        health="BLOCKED · position mismatch",
        venue_positions={},
        position_truth_healthy=False,
    )

    assert "⚠️ Local journal reduction BUSDT" in update.message
    assert "awaiting venue reconciliation" in update.message
    assert "✅ Closed BUSDT" not in update.message
    assert "✅ Reduced BUSDT" not in update.message
    assert len(update.next_state.pending_lifecycle_confirmations) == 1
    notifier.commit(update)

    confirmed = notifier.prepare(
        midpoint_by_symbol={},
        health="healthy",
        venue_positions={},
        position_truth_healthy=True,
    )
    assert "✅ Venue reconciliation confirmed prior update" in confirmed.message
    assert "Closed BUSDT · take profit" in confirmed.message
    assert confirmed.next_state.pending_lifecycle_confirmations == {}
    # Delivery state remains transactional: no commit means the confirmation is
    # prepared again, while a successful commit retires it exactly once.
    assert (
        notifier.prepare(
            midpoint_by_symbol={},
            health="healthy",
            venue_positions={},
            position_truth_healthy=True,
        ).message
        == confirmed.message
    )
    notifier.commit(confirmed)
    assert (
        notifier.prepare(
            midpoint_by_symbol={},
            health="healthy",
            venue_positions={},
            position_truth_healthy=True,
        ).message
        == ""
    )


def test_schema_two_notification_state_migrates_without_replaying_history(
    tmp_path: Path,
) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    state_path = tmp_path / "notify-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "last_sequence": kernel.state().events_applied,
                "last_hour_bucket": 10,
                "positions": {
                    "BUSDT": {"signed_qty": 2.0, "average_price": 10.0},
                },
                "loss_level_by_symbol": {},
                "entry_rejections": {},
                "recent_entry_rejection_count": 0,
                "recent_entry_rejection_attempts": {},
                "recent_entry_rejection_reasons": {},
            }
        )
    )
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=state_path,
        clock=clock,
    )

    migrated = notifier.prepare(
        midpoint_by_symbol={"BUSDT": 10.0},
        health="healthy",
    )

    assert migrated.message == ""
    assert migrated.next_state.schema_version == 3
    assert migrated.next_state.last_sequence == kernel.state().events_applied


def test_native_stop_breach_and_software_recovery_are_immediately_visible(
    tmp_path: Path,
) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )
    notifier.commit(notifier.prepare(midpoint_by_symbol={"BUSDT": 10.0}, health="healthy"))
    kernel.record_protection(
        protection_key="native:BUSDT",
        symbol="BUSDT",
        status="breached_unprotected",
        stop_price=9.0,
        take_profit_price=None,
        exchange_ts_ns=0,
        local_receive_ts_ns=clock.wall_time_ns(),
        metadata={
            "native_exchange": True,
            "symbol": "BUSDT",
            "breach_mark": 8.9,
        },
    )
    kernel.record_protection(
        protection_key="protection:native-breach:BUSDT:test",
        symbol="BUSDT",
        status="software_flat_requested",
        stop_price=9.0,
        take_profit_price=None,
        exchange_ts_ns=0,
        local_receive_ts_ns=clock.wall_time_ns(),
        metadata={"native_exchange": False},
    )

    update = notifier.prepare(
        midpoint_by_symbol={"BUSDT": 8.9},
        health="BLOCKED · native protection breach",
    )

    assert "🚨 BUSDT native disaster stop absent after threshold breach" in update.message
    assert "new exposure blocked; software flat recovery required" in update.message
    assert "🛡️ BUSDT durable reduce-only recovery queued" in update.message


def test_fresh_notifier_surfaces_existing_unresolved_native_breach(
    tmp_path: Path,
) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    kernel.record_protection(
        protection_key="native:BUSDT",
        symbol="BUSDT",
        status="breached_unprotected",
        stop_price=9.0,
        take_profit_price=None,
        exchange_ts_ns=0,
        local_receive_ts_ns=clock.wall_time_ns(),
        metadata={
            "native_exchange": True,
            "symbol": "BUSDT",
            "breach_mark": 8.9,
        },
    )
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "new-notify-state.json",
        clock=clock,
    )

    update = notifier.prepare(
        midpoint_by_symbol={"BUSDT": 8.9},
        health="BLOCKED · native protection breach",
    )

    assert "🚨 BUSDT native disaster stop absent after threshold breach" in update.message


def test_hourly_open_position_without_fresh_midpoint_reports_unknown_valuation(
    tmp_path: Path,
) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )

    report = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert "L2 midpoint/notional/estimated uPnL unavailable" in report.message
    assert "L2 midpoint valuation unavailable: BUSDT" in report.message
    assert "Account execution health: BLOCKED · L2 midpoint valuation unavailable" in report.message
    assert "BUSDT long 2 · $20.00 · uPnL +$0.00" not in report.message


def test_native_protection_failure_does_not_claim_position_truth_mismatch(
    tmp_path: Path,
) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )

    report = notifier.prepare(
        midpoint_by_symbol={"BUSDT": 10.0},
        health="BLOCKED · native protection missing",
        venue_positions={"BUSDT": 2.0},
        position_truth_healthy=True,
    )

    assert "Position truth mismatch" not in report.message
    assert "BUSDT long 2" in report.message
    assert "Account execution health: BLOCKED · native protection missing" in report.message


@pytest.mark.parametrize(
    ("position_truth_healthy", "position_truth_status"),
    [(True, "healthy"), (False, "mismatch")],
)
def test_hourly_summary_explicitly_separates_continuous_gate_from_account_health(
    tmp_path: Path,
    position_truth_healthy: bool,
    position_truth_status: str,
) -> None:
    kernel, clock, *_ = _setup_open(tmp_path / "account")
    notifier = AccountNotificationEngine(
        kernel=kernel,
        state_path=tmp_path / "notify-state.json",
        clock=clock,
    )
    continuous_status = (
        "CONTINUOUS BTC gate: BLOCKED · uptrend · 30d -0.44%\n"
        "CONTINUOUS funnel (component opportunities): "
        "D9 3 → liquidity 2 → event 2 → age 1 → capacity 1\n"
        "CONTINUOUS qualified but blocked: AAAUSDT · "
        "first rejection btc trend gate"
    )

    report = notifier.prepare(
        midpoint_by_symbol={"BUSDT": 10.0},
        health=("healthy" if position_truth_healthy else "BLOCKED · account reconciliation mismatch"),
        venue_positions={"BUSDT": 2.0},
        position_truth_healthy=position_truth_healthy,
        position_truth_status=position_truth_status,
        continuous_status=continuous_status,
    )

    assert continuous_status in report.message
    assert "Account execution health:" in report.message
    assert "\nExecution health:" not in report.message


def test_entry_risk_first_rejection_sends_one_actionable_alert(tmp_path: Path) -> None:
    kernel, _, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    result = _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-first",
    )

    update = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert not result.accepted
    assert len(update.event_messages) == 1
    assert "⚠️ Entry blocked by account risk · Bybit demo" in update.message
    assert "BUSDT · component trade-a" in update.message
    assert "Reasons: below min notional" in update.message
    assert "No order was sent" in update.message
    assert "risk-first" not in update.message
    assert update.next_state.entry_rejections["attempt:trade-a"]["count"] == 1


def test_identical_entry_risk_rejections_increment_durably_without_spam(
    tmp_path: Path,
) -> None:
    kernel, _, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-repeat-1",
    )
    notifier.commit(notifier.prepare(midpoint_by_symbol={}, health="healthy"))
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-repeat-2",
    )

    repeated = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert repeated.event_messages == ()
    assert repeated.message == ""
    assert repeated.next_state.entry_rejections["attempt:trade-a"]["count"] == 2
    assert repeated.next_state.recent_entry_rejection_count == 2
    notifier.commit(repeated)
    persisted = json.loads(notifier.state_path.read_text())
    assert persisted["entry_rejections"]["attempt:trade-a"]["count"] == 2


def test_expired_entry_signal_does_not_remain_an_unresolved_risk_block(
    tmp_path: Path,
) -> None:
    kernel, clock, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    valid_until_ms = clock.wall_time_ns() // 1_000_000 + 1_000
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-expiring",
        signal_valid_until_ms=valid_until_ms,
    )
    first = notifier.prepare(midpoint_by_symbol={}, health="healthy")
    assert first.next_state.entry_rejections["attempt:trade-a"]["signal_valid_until_ns"] == valid_until_ms * 1_000_000
    notifier.commit(first)

    clock.advance_ns(2_000_000_000)
    expired = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert expired.next_state.entry_rejections == {}
    assert "unresolved attempt" not in expired.message


def test_entry_risk_reason_change_sends_one_changed_alert(tmp_path: Path) -> None:
    kernel, _, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-change-1",
    )
    notifier.commit(notifier.prepare(midpoint_by_symbol={}, health="healthy"))
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-change-2",
        rejection="component_gross_limit",
    )

    changed = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert len(changed.event_messages) == 1
    assert "⚠️ Entry risk block changed · Bybit demo" in changed.message
    assert "Reasons: component gross limit" in changed.message
    assert "below min notional" not in changed.message
    assert changed.next_state.entry_rejections["attempt:trade-a"]["reasons"] == ["component_gross_limit"]


def test_later_accepted_entry_risk_clears_active_block_once(tmp_path: Path) -> None:
    kernel, _, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-resolve-1",
    )
    notifier.commit(notifier.prepare(midpoint_by_symbol={}, health="healthy"))
    accepted = _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-resolve-2",
        rejection=None,
    )

    resolved = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert accepted.accepted
    assert len(resolved.event_messages) == 1
    assert "✅ Entry risk block cleared · Bybit demo" in resolved.message
    assert "after 1 rejected evaluation" in resolved.message
    assert "execution/fill is still pending" in resolved.message
    assert resolved.next_state.entry_rejections == {}
    notifier.commit(resolved)
    assert notifier.prepare(midpoint_by_symbol={}, health="healthy").message == ""


def test_multiple_entry_components_in_one_batch_are_grouped(tmp_path: Path) -> None:
    kernel, _, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-grouped",
        components=("trade-a", "trade-b"),
    )

    update = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert len(update.event_messages) == 1
    assert "BUSDT · 2 components" in update.message
    assert len(update.next_state.entry_rejections) == 2
    assert update.next_state.recent_entry_rejection_count == 2
    assert update.next_state.recent_entry_rejection_reasons == {"below_min_notional": 2}


def test_failed_entry_risk_alert_delivery_leaves_state_unchanged(tmp_path: Path) -> None:
    kernel, _, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    before = notifier.state_path.read_bytes()
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-unsent",
    )

    unsent = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert "Entry blocked by account risk" in unsent.message
    assert notifier.state_path.read_bytes() == before
    retry = notifier.prepare(midpoint_by_symbol={}, health="healthy")
    assert retry.message == unsent.message
    assert retry.next_state.entry_rejections["attempt:trade-a"]["count"] == 1


def test_notification_pagination_preserves_every_section_without_oversize_pages() -> None:
    sections = tuple(f"event-{index}:" + (str(index) * 700) for index in range(10))

    pages = account_notifications_module._paginate_sections(sections)

    assert len(pages) > 1
    assert all(0 < len(page) <= 3900 for page in pages)
    rendered = "\n\n".join(pages)
    assert "additional account updates omitted" not in rendered
    for section in sections:
        assert section in rendered


def test_hourly_summary_aggregates_entry_risk_repeats(tmp_path: Path) -> None:
    kernel, clock, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    for index in (1, 2):
        _submit_entry_risk_decision(
            kernel,
            snapshot=snapshot,
            loose_policy=policy,
            batch_id=f"risk-hourly-{index}",
        )
        notifier.commit(notifier.prepare(midpoint_by_symbol={}, health="healthy"))
    clock.advance_ns(HOUR_NS)

    hourly = notifier.prepare(midpoint_by_symbol={}, health="healthy")

    assert hourly.hourly_included
    assert "Entry risk: 1 unresolved attempt(s)" in hourly.message
    assert "2 rejected evaluation(s) across 1 attempt(s)" in hourly.message
    assert "below min notional ×2" in hourly.message
    assert "risk-hourly" not in hourly.message
    assert hourly.next_state.recent_entry_rejection_count == 0
    assert hourly.next_state.recent_entry_rejection_attempts == {}
    assert hourly.next_state.recent_entry_rejection_reasons == {}
    assert len(hourly.next_state.entry_rejections) == 1


def test_entry_risk_dedupe_and_fallback_attempt_key_survive_restart(
    tmp_path: Path,
) -> None:
    kernel, _, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-restart-1",
        explicit_attempt_keys=False,
    )
    notifier.commit(notifier.prepare(midpoint_by_symbol={}, health="healthy"))
    restarted = AccountNotificationEngine(
        kernel=kernel,
        state_path=notifier.state_path,
        clock=notifier.clock,
    )
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-restart-2",
        explicit_attempt_keys=False,
    )

    repeated = restarted.prepare(midpoint_by_symbol={}, health="healthy")

    fallback_key = "entry:continuous/notify/trade-a/BUSDT"
    assert repeated.message == ""
    assert repeated.next_state.entry_rejections[fallback_key]["count"] == 2


def test_first_notification_run_does_not_replay_old_entry_risk_rejections(
    tmp_path: Path,
) -> None:
    kernel, _, notifier, snapshot, policy = _setup_risk_notifications(tmp_path / "account")
    notifier.state_path.unlink()
    _submit_entry_risk_decision(
        kernel,
        snapshot=snapshot,
        loose_policy=policy,
        batch_id="risk-before-first-run",
    )
    fresh = AccountNotificationEngine(
        kernel=kernel,
        state_path=notifier.state_path,
        clock=notifier.clock,
    )

    first = fresh.prepare(midpoint_by_symbol={}, health="healthy")

    assert first.hourly_included
    assert "Entry blocked by account risk" not in first.message
    assert "Entry risk: 1 unresolved attempt(s)" in first.message
    assert "active reasons below min notional" in first.message
    assert len(first.next_state.entry_rejections) == 1
