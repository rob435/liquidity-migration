from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    AccountState,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    TargetBatchResult,
    read_account_journal,
)
from liquidity_migration.account_strategy_state import (
    canonical_account_projection,
    canonical_adverse_reduction_events,
    canonical_component_execution_anchors,
    canonical_entry_attempts,
    canonical_reduction_events,
    canonical_strategy_trade_rows,
    target_reservation_rows,
    terminal_entry_attempt_keys,
)
from liquidity_migration.continuous_btc_risk import BTC_RISK_EVIDENCE_METADATA_KEY
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.entry_attempts import ENTRY_ATTEMPT_METADATA_KEY, entry_attempt_key


def _submit(
    kernel: AccountExecutionKernel,
    *,
    batch: str,
    qty: float,
    reason: str,
    reference_price: float = 10.0,
    metadata: dict[str, object] | None = None,
    target_key: str = "long/strategy/trade-1/BUSDT",
    component_id: str = "trade-1",
) -> TargetBatchResult:
    target_metadata: dict[str, object] = {
        "signal_ts_ms": 123,
        "stop_price": 9.0,
        "take_profit_price": 12.0,
    }
    target_metadata.update(metadata or {})
    return kernel.submit_targets(
        batch_id=batch,
        market_inputs=[MarketInputRef(f"book:{batch}", "BUSDT", 1, 2, 10.0)],
        targets=[DesiredTarget(
            decision_key=f"decision:{batch}",
            target_key=target_key,
            sleeve="long",
            strategy_id="strategy",
            component_id=component_id,
            symbol="BUSDT",
            signed_qty=qty,
            reference_price=reference_price,
            leverage=10.0,
            reason=reason,
            metadata=target_metadata,
        )],
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 3),
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
    )


def _submit_group(
    kernel: AccountExecutionKernel,
    *,
    batch: str,
    targets: list[tuple[str, str, float]],
    reason: str,
    metadata: dict[str, object] | None = None,
    reference_price: float = 10.0,
) -> TargetBatchResult:
    return kernel.submit_targets(
        batch_id=batch,
        market_inputs=[
            MarketInputRef(
                f"book:{batch}",
                "BUSDT",
                kernel.clock.wall_time_ns() - 1,
                kernel.clock.wall_time_ns(),
                reference_price,
            )
        ],
        targets=[
            DesiredTarget(
                decision_key=f"decision:{batch}:{component_id}",
                target_key=target_key,
                sleeve="long",
                strategy_id="strategy",
                component_id=component_id,
                symbol="BUSDT",
                signed_qty=qty,
                reference_price=reference_price,
                leverage=10.0,
                reason=reason,
                metadata=dict(metadata or {}),
            )
            for target_key, component_id, qty in targets
        ],
        risk_snapshot=AccountRiskSnapshot(
            10_000.0,
            9_000.0,
            f"wallet:{batch}",
            kernel.clock.wall_time_ns(),
        ),
        risk_policy=AccountRiskPolicy(
            10_000.0,
            10_000.0,
            10_000.0,
            10_000.0,
            10.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0),
        },
    )


def _ack_and_fill(
    kernel: AccountExecutionKernel,
    result: TargetBatchResult,
    *,
    fills: list[tuple[str, float, float, int]],
) -> None:
    command = result.commands[0]
    first_ts_ns = fills[0][3]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id=f"venue:{result.batch_id}",
        exchange_ts_ns=first_ts_ns - 2,
        local_ack_ts_ns=first_ts_ns - 1,
    )
    for execution_id, signed_qty, price, receive_ts_ns in fills:
        kernel.record_fill(
            command_id=command.command_id,
            execution_id=execution_id,
            signed_qty=signed_qty,
            price=price,
            fee_usdt=0.01,
            exchange_ts_ns=receive_ts_ns - 1,
            local_receive_ts_ns=receive_ts_ns,
            metadata={
                "fee_observed": True,
                "fee_status": "observed_execution_fee",
                "fee_source": "test",
            },
        )


def test_projection_waits_for_reconstructed_position_convergence(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    kernel = AccountExecutionKernel(tmp_path / "account", account_id="a", clock=clock)
    opened = _submit(kernel, batch="open", qty=2.0, reason="entry")
    projected = canonical_strategy_trade_rows(
        tmp_path / "account", sleeve="long", strategy_ids=("strategy",)
    )
    assert projected.select(
        "trade_id",
        "status",
        "qty",
        "stop_price",
        "account_target_status",
        "account_target_signed_qty",
        "account_position_signed_qty",
    ).to_dicts() == [{
        "trade_id": "trade-1",
        "status": "target_pending",
        "qty": 2.0,
        "stop_price": 9.0,
        "account_target_status": "pending",
        "account_target_signed_qty": 2.0,
        "account_position_signed_qty": 0.0,
    }]

    open_command = opened.commands[0]
    kernel.record_ack(
        command_id=open_command.command_id,
        accepted=True,
        venue_order_id="venue-open",
        exchange_ts_ns=1_000_100_000,
        local_ack_ts_ns=1_000_200_000,
    )
    kernel.record_fill(
        command_id=open_command.command_id,
        execution_id="fill-open",
        signed_qty=2.0,
        price=10.5,
        fee_usdt=0.01,
        exchange_ts_ns=1_000_300_000,
        local_receive_ts_ns=1_000_400_000,
    )
    projected = canonical_strategy_trade_rows(
        tmp_path / "account", sleeve="long", strategy_ids=("strategy",)
    )
    assert projected.select(
        "status", "account_target_status", "account_position_signed_qty", "entry_price"
    ).to_dicts() == [{
        "status": "open",
        "account_target_status": "converged",
        "account_position_signed_qty": 2.0,
        "entry_price": 10.5,
    }]

    clock.advance_ns(1_000_000)
    closed = _submit(kernel, batch="close", qty=0.0, reason="time_stop")
    projected = canonical_strategy_trade_rows(
        tmp_path / "account", sleeve="long", strategy_ids=("strategy",)
    )
    assert projected["status"].to_list() == ["target_pending"]
    assert projected["target_action"].to_list() == ["close"]
    assert projected["exit_ts_ms"].to_list() == [None]
    assert projected["closed_at_ms"].to_list() == [None]
    assert projected["cooldown_start_ts_ms"].to_list() == [None]
    assert projected["exit_target_ts_ms"].to_list() == [1_001]
    assert "exit_reason" not in projected.columns

    close_command = closed.commands[0]
    kernel.record_ack(
        command_id=close_command.command_id,
        accepted=True,
        venue_order_id="venue-close",
        exchange_ts_ns=1_001_100_000,
        local_ack_ts_ns=1_001_200_000,
    )
    kernel.record_fill(
        command_id=close_command.command_id,
        execution_id="fill-close",
        signed_qty=-2.0,
        price=10.7,
        fee_usdt=0.01,
        exchange_ts_ns=1_001_300_000,
        local_receive_ts_ns=1_001_400_000,
    )
    projected = canonical_strategy_trade_rows(
        tmp_path / "account", sleeve="long", strategy_ids=("strategy",)
    )
    assert projected["status"].to_list() == ["closed"]
    assert projected["account_target_status"].to_list() == ["converged"]
    assert projected["exit_reason"].to_list() == ["time_stop"]

def test_target_reservations_include_pending_without_relabelling_it_open() -> None:
    rows = pl.DataFrame([
        {"trade_id": "filled", "status": "open"},
        {"trade_id": "pending-entry", "status": "target_pending"},
        {"trade_id": "pending-close", "status": "TARGET_PENDING"},
        {"trade_id": "done", "status": "closed"},
    ])

    reserved = target_reservation_rows(rows)

    assert reserved["trade_id"].to_list() == [
        "filled",
        "pending-entry",
        "pending-close",
    ]
    assert reserved["status"].to_list() == [
        "open",
        "target_pending",
        "TARGET_PENDING",
    ]


def test_entry_attempt_projection_includes_risk_rejections_without_trade_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a")
    rejected_target_key = "long/strategy/rejected-signal/BUSDT"
    rejected_attempt_key = entry_attempt_key(rejected_target_key)
    rejected = _submit(
        kernel,
        batch="rejected-entry",
        qty=200.0,
        reason="entry",
        target_key=rejected_target_key,
        component_id="rejected-signal",
        metadata={
            ENTRY_ATTEMPT_METADATA_KEY: rejected_attempt_key,
            "signal_ts_ms": 100,
            "signal_valid_until_ms": 200,
        },
    )
    assert not rejected.accepted

    projected = canonical_entry_attempts(
        root,
        sleeve="long",
        strategy_ids=("strategy",),
    )
    assert len(projected) == 1
    attempt = projected[0]
    assert attempt.entry_attempt_key == rejected_attempt_key
    assert attempt.target_key == rejected_target_key
    assert attempt.batch_id == "rejected-entry"
    assert not attempt.accepted
    assert attempt.rejection_keys
    assert attempt.risk_decision_sequence > attempt.target_event_sequence
    assert canonical_strategy_trade_rows(
        root, sleeve="long", strategy_ids=("strategy",)
    ).is_empty()
    assert terminal_entry_attempt_keys(
        root,
        sleeve="long",
        strategy_ids=("strategy",),
    ) == frozenset({rejected_attempt_key})

    # A fresh process reconstructs the same terminal attempt from the journal.
    AccountExecutionKernel(root, account_id="a")
    assert canonical_entry_attempts(root, sleeve="long") == projected


def test_new_signal_attempt_is_distinct_from_prior_risk_rejection(tmp_path: Path) -> None:
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a")
    old_key = "long/strategy/old-signal/BUSDT"
    new_key = "long/strategy/new-signal/BUSDT"
    _submit(
        kernel,
        batch="old-rejected",
        qty=200.0,
        reason="entry",
        target_key=old_key,
        component_id="old-signal",
        metadata={
            ENTRY_ATTEMPT_METADATA_KEY: entry_attempt_key(old_key),
            "signal_ts_ms": 100,
            "signal_valid_until_ms": 200,
        },
    )

    terminal = terminal_entry_attempt_keys(root, sleeve="long")
    assert entry_attempt_key(old_key) in terminal
    assert entry_attempt_key(new_key) not in terminal


def test_execution_rejection_remains_accepted_convergence_state_not_terminal_attempt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a")
    target_key = "long/strategy/execution-reject/BUSDT"
    submitted = _submit(
        kernel,
        batch="accepted-before-execution-reject",
        qty=2.0,
        reason="entry",
        target_key=target_key,
        component_id="execution-reject",
        metadata={
            ENTRY_ATTEMPT_METADATA_KEY: entry_attempt_key(target_key),
            "signal_ts_ms": 100,
            "signal_valid_until_ms": 200,
        },
    )
    assert submitted.accepted
    command = submitted.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=False,
        venue_order_id="",
        rejection_key="venue:definite-reject",
        exchange_ts_ns=10,
        local_ack_ts_ns=11,
    )

    attempts = canonical_entry_attempts(root, sleeve="long")
    assert len(attempts) == 1 and attempts[0].accepted
    assert terminal_entry_attempt_keys(root, sleeve="long") == frozenset()
    rows = canonical_strategy_trade_rows(root, sleeve="long")
    assert rows["status"].to_list() == ["target_pending"]


def test_close_target_retains_prior_entry_btc_risk_evidence(tmp_path: Path) -> None:
    kernel = AccountExecutionKernel(tmp_path / "account", account_id="a")
    evidence = {
        "schema_version": 1,
        "decision_key": "BUSDT|123",
        "evidence_hash": "a" * 64,
    }
    _submit(
        kernel,
        batch="open-with-evidence",
        qty=2.0,
        reason="entry",
        metadata={BTC_RISK_EVIDENCE_METADATA_KEY: evidence},
    )
    _submit(kernel, batch="close-without-evidence", qty=0.0, reason="exit")

    projected = canonical_strategy_trade_rows(
        tmp_path / "account", sleeve="long", strategy_ids=("strategy",)
    )

    assert projected[BTC_RISK_EVIDENCE_METADATA_KEY].to_list() == [evidence]


@pytest.mark.parametrize("terminal_status", ["rejected", "cancelled"])
def test_terminal_order_does_not_release_still_desired_target_reservation(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    kernel = AccountExecutionKernel(tmp_path / terminal_status, account_id="a", clock=clock)
    submitted = _submit(kernel, batch="open", qty=2.0, reason="entry")
    command = submitted.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id=f"venue-{terminal_status}",
        exchange_ts_ns=1_000_100_000,
        local_ack_ts_ns=1_000_200_000,
    )
    kernel.record_order_status(
        command_id=command.command_id,
        status=terminal_status,
        cumulative_filled_qty=0.0,
        exchange_ts_ns=1_000_300_000,
        local_receive_ts_ns=1_000_400_000,
    )

    projected = canonical_strategy_trade_rows(
        tmp_path / terminal_status,
        sleeve="long",
        strategy_ids=("strategy",),
    )

    assert projected.select("status", "account_working_order_count").to_dicts() == [{
        "status": "target_pending",
        "account_working_order_count": 0,
    }]
    assert target_reservation_rows(projected)["trade_id"].to_list() == ["trade-1"]


def test_projection_ignores_owner_convergence_retry_target_metadata(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    kernel = AccountExecutionKernel(tmp_path / "account", account_id="a", clock=clock)
    opened = _submit(kernel, batch="open", qty=2.0, reason="strategy_entry")
    open_command = opened.commands[0]
    kernel.record_ack(
        command_id=open_command.command_id,
        accepted=True,
        venue_order_id="venue-open",
        exchange_ts_ns=1_000_100_000,
        local_ack_ts_ns=1_000_200_000,
    )
    kernel.record_fill(
        command_id=open_command.command_id,
        execution_id="fill-open",
        signed_qty=2.0,
        price=10.5,
        fee_usdt=0.01,
        exchange_ts_ns=1_000_300_000,
        local_receive_ts_ns=1_000_400_000,
    )

    clock.advance_ns(1_000_000)
    retry = _submit(
        kernel,
        batch="account-convergence/BUSDT/generation/001",
        qty=2.0,
        reason="synthetic_retry",
        reference_price=99.0,
        metadata={
            "account_convergence_retry": True,
            "stop_price": 1.0,
            "take_profit_price": 100.0,
        },
    )
    assert retry.accepted
    assert retry.commands == ()

    projected = canonical_strategy_trade_rows(
        tmp_path / "account", sleeve="long", strategy_ids=("strategy",)
    )

    assert projected.select(
        "status",
        "target_reason",
        "target_reference_price",
        "stop_price",
        "take_profit_price",
    ).to_dicts() == [{
        "status": "open",
        "target_reason": "strategy_entry",
        "target_reference_price": 10.0,
        "stop_price": 9.0,
        "take_profit_price": 12.0,
    }]


def test_convergence_retry_entry_uses_retry_fill_and_original_target_clock(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    opened = _submit(
        kernel,
        batch="strategy-entry",
        qty=2.0,
        reason="entry",
        metadata={"max_hold_duration_ms": 3_600_000},
    )
    kernel.record_ack(
        command_id=opened.commands[0].command_id,
        accepted=False,
        venue_order_id="",
        rejection_key="venue:definite-reject",
        exchange_ts_ns=1_100_000_000,
        local_ack_ts_ns=1_200_000_000,
    )

    clock.advance_to_wall_ns(3_000_000_000)
    retry = _submit(
        kernel,
        batch="account-convergence/BUSDT/generation/entry-001",
        qty=2.0,
        reason="synthetic_retry",
        reference_price=11.0,
        metadata={"account_convergence_retry": True},
    )
    assert len(retry.commands) == 1
    _ack_and_fill(
        kernel,
        retry,
        fills=[("retry-entry-fill", 2.0, 11.25, 4_000_000_000)],
    )

    anchor = canonical_component_execution_anchors(root, sleeve="long")[0]
    assert anchor.entry_target_batch_id == "strategy-entry"
    assert anchor.entry_execution_batch_id == retry.batch_id
    assert anchor.entry_target_ts_ms == 1_000
    assert anchor.entry_fill_ts_ms == 4_000
    assert anchor.entry_fill_execution_id == "retry-entry-fill"
    assert anchor.entry_attribution_basis == "component_convergence_retry_first_fill"

    row = canonical_strategy_trade_rows(root, sleeve="long").to_dicts()[0]
    assert row["status"] == "open"
    assert row["entry_target_batch_id"] == "strategy-entry"
    assert row["entry_execution_batch_id"] == retry.batch_id
    assert row["entry_target_ts_ms"] == 1_000
    assert row["entry_ts_ms"] == 4_000
    assert row["target_reason"] == "entry"
    assert row["max_hold_deadline_ts_ms"] == 4_000 + 3_600_000


def test_convergence_retry_close_uses_original_target_and_retry_terminal_fill(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    opened = _submit(kernel, batch="strategy-entry", qty=2.0, reason="entry")
    _ack_and_fill(
        kernel,
        opened,
        fills=[("entry-fill", 2.0, 10.0, 2_000_000_000)],
    )

    clock.advance_to_wall_ns(3_000_000_000)
    closed = _submit(
        kernel,
        batch="strategy-close",
        qty=0.0,
        reason="max_hold",
        reference_price=9.0,
    )
    kernel.record_ack(
        command_id=closed.commands[0].command_id,
        accepted=False,
        venue_order_id="",
        rejection_key="venue:definite-reject",
        exchange_ts_ns=3_100_000_000,
        local_ack_ts_ns=3_200_000_000,
    )

    clock.advance_to_wall_ns(4_000_000_000)
    retry = _submit(
        kernel,
        batch="account-convergence/BUSDT/generation/close-001",
        qty=0.0,
        reason="synthetic_retry",
        reference_price=9.0,
        metadata={"account_convergence_retry": True},
    )
    assert len(retry.commands) == 1
    _ack_and_fill(
        kernel,
        retry,
        fills=[("retry-close-fill", -2.0, 9.0, 5_000_000_000)],
    )
    kernel.finalize_flat_position(
        symbol="BUSDT",
        command_id=retry.commands[0].command_id,
        exchange_ts_ns=4_999_999_999,
        local_receive_ts_ns=5_000_000_000,
        reason="max_hold",
    )

    anchor = canonical_component_execution_anchors(root, sleeve="long")[0]
    assert anchor.close_target_batch_id == "strategy-close"
    assert anchor.close_execution_batch_id == retry.batch_id
    assert anchor.close_target_ts_ms == 3_000
    assert anchor.close_fill_ts_ms == 5_000
    assert anchor.close_fill_execution_id == "retry-close-fill"
    assert anchor.close_attribution_basis == (
        "terminal_component_convergence_retry_fill"
    )

    reductions = canonical_reduction_events(root, sleeve="long")
    assert len(reductions) == 1
    assert reductions[0].batch_id == retry.batch_id
    row = canonical_strategy_trade_rows(root, sleeve="long").to_dicts()[0]
    assert row["status"] == "closed"
    assert row["exit_target_ts_ms"] == 3_000
    assert row["exit_execution_batch_id"] == retry.batch_id
    assert row["exit_ts_ms"] == 5_000
    assert row["cooldown_start_ts_ms"] == 5_000
    assert row["exit_pnl_key"] == reductions[0].pnl_key
    assert row["exit_reason"] == "max_hold"


def test_convergence_retry_close_combines_partial_original_reduction(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    opened = _submit(kernel, batch="strategy-entry", qty=2.0, reason="entry")
    _ack_and_fill(
        kernel,
        opened,
        fills=[("entry-fill", 2.0, 10.0, 2_000_000_000)],
    )

    clock.advance_to_wall_ns(3_000_000_000)
    closed = _submit(
        kernel,
        batch="strategy-close",
        qty=0.0,
        reason="max_hold",
        reference_price=9.0,
    )
    _ack_and_fill(
        kernel,
        closed,
        fills=[("partial-close-fill", -0.5, 9.5, 3_500_000_000)],
    )
    kernel.record_order_status(
        command_id=closed.commands[0].command_id,
        status="cancelled",
        cumulative_filled_qty=0.5,
        exchange_ts_ns=3_599_999_999,
        local_receive_ts_ns=3_600_000_000,
    )

    clock.advance_to_wall_ns(4_000_000_000)
    retry = _submit(
        kernel,
        batch="account-convergence/BUSDT/generation/close-residual-001",
        qty=0.0,
        reason="synthetic_retry",
        reference_price=9.0,
        metadata={"account_convergence_retry": True},
    )
    assert retry.commands[0].signed_qty == pytest.approx(-1.5)
    _ack_and_fill(
        kernel,
        retry,
        fills=[("retry-close-fill", -1.5, 9.0, 5_000_000_000)],
    )
    kernel.finalize_flat_position(
        symbol="BUSDT",
        command_id=retry.commands[0].command_id,
        exchange_ts_ns=4_999_999_999,
        local_receive_ts_ns=5_000_000_000,
        reason="max_hold",
    )

    anchor = canonical_component_execution_anchors(root, sleeve="long")[0]
    assert anchor.close_target_batch_id == "strategy-close"
    assert anchor.close_execution_batch_id == retry.batch_id
    assert anchor.close_fill_ts_ms == 5_000
    assert anchor.close_observed_signed_qty == pytest.approx(-2.0)
    assert anchor.close_fill_vwap == pytest.approx(9.125)
    assert canonical_strategy_trade_rows(root, sleeve="long")["status"].to_list() == [
        "closed"
    ]


def test_projection_stays_pending_while_superseded_order_is_working(tmp_path: Path) -> None:
    kernel = AccountExecutionKernel(tmp_path / "account", account_id="a")
    opened = _submit(kernel, batch="open", qty=2.0, reason="entry")
    open_command = opened.commands[0]
    kernel.record_ack(
        command_id=open_command.command_id,
        accepted=True,
        venue_order_id="venue-open",
        exchange_ts_ns=10,
        local_ack_ts_ns=11,
    )
    kernel.record_fill(
        command_id=open_command.command_id,
        execution_id="fill-open",
        signed_qty=2.0,
        price=10.0,
        fee_usdt=0.0,
        exchange_ts_ns=12,
        local_receive_ts_ns=13,
    )
    older_resize = _submit(kernel, batch="resize-three", qty=3.0, reason="resize")
    assert len(older_resize.commands) == 1

    latest = _submit(kernel, batch="supersede-two", qty=2.0, reason="supersede")
    assert latest.accepted
    assert latest.commands == ()
    projected = canonical_strategy_trade_rows(
        tmp_path / "account", sleeve="long", strategy_ids=("strategy",)
    )
    assert projected.select(
        "status",
        "account_target_signed_qty",
        "account_position_signed_qty",
        "account_working_signed_qty",
        "account_working_order_count",
        "account_projected_signed_qty",
    ).to_dicts() == [{
        "status": "open",
        "account_target_signed_qty": 2.0,
        "account_position_signed_qty": 2.0,
        "account_working_signed_qty": 1.0,
        "account_working_order_count": 1,
        "account_projected_signed_qty": 3.0,
    }]

    kernel.record_ack(
        command_id=older_resize.commands[0].command_id,
        accepted=False,
        venue_order_id="",
        rejection_key="cancelled-before-submit",
        exchange_ts_ns=14,
        local_ack_ts_ns=15,
    )
    projected = canonical_strategy_trade_rows(
        tmp_path / "account", sleeve="long", strategy_ids=("strategy",)
    )
    assert projected.select(
        "status", "account_target_status", "account_working_order_count"
    ).to_dicts() == [{
        "status": "open",
        "account_target_status": "converged",
        "account_working_order_count": 0,
    }]


@pytest.mark.parametrize("second_order_status", ["working", "rejected"])
def test_filled_component_remains_exit_visible_while_same_symbol_peer_is_pending(
    tmp_path: Path,
    second_order_status: str,
) -> None:
    from liquidity_migration.continuous_demo import _open_continuous_trades
    from liquidity_migration.long_native_event_demo import _open_long_trades

    kernel = AccountExecutionKernel(tmp_path / second_order_status, account_id="a")
    component_a = _submit(
        kernel,
        batch="open-a",
        qty=2.0,
        reason="entry_a",
        target_key="long/strategy/trade-a/BUSDT",
        component_id="trade-a",
    )
    command_a = component_a.commands[0]
    kernel.record_ack(
        command_id=command_a.command_id,
        accepted=True,
        venue_order_id="venue-a",
        exchange_ts_ns=10,
        local_ack_ts_ns=11,
    )
    kernel.record_fill(
        command_id=command_a.command_id,
        execution_id="fill-a",
        signed_qty=2.0,
        price=10.5,
        fee_usdt=0.0,
        exchange_ts_ns=12,
        local_receive_ts_ns=13,
    )

    component_b = _submit(
        kernel,
        batch="open-b",
        qty=1.0,
        reason="entry_b",
        target_key="long/strategy/trade-b/BUSDT",
        component_id="trade-b",
    )
    command_b = component_b.commands[0]
    kernel.record_ack(
        command_id=command_b.command_id,
        accepted=second_order_status == "working",
        venue_order_id="venue-b" if second_order_status == "working" else "",
        rejection_key=(
            "venue-rejected" if second_order_status == "rejected" else ""
        ),
        exchange_ts_ns=14,
        local_ack_ts_ns=15,
    )

    projected = canonical_strategy_trade_rows(
        tmp_path / second_order_status,
        sleeve="long",
        strategy_ids=("strategy",),
    ).sort("trade_id")

    assert projected.select(
        "trade_id",
        "status",
        "account_target_status",
        "account_symbol_target_status",
        "account_component_convergence_basis",
        "account_position_attribution_status",
    ).to_dicts() == [
        {
            "trade_id": "trade-a",
            "status": "open",
            "account_target_status": "converged",
            "account_symbol_target_status": "pending",
            "account_component_convergence_basis": "component_batch_fill",
                "account_position_attribution_status": "component_fill_attributed",
        },
        {
            "trade_id": "trade-b",
            "status": "target_pending",
            "account_target_status": "pending",
            "account_symbol_target_status": "pending",
            "account_component_convergence_basis": "pending_component_batch",
                "account_position_attribution_status": "pending_first_fill",
        },
    ]
    assert set(target_reservation_rows(projected)["trade_id"].to_list()) == {
        "trade-a",
        "trade-b",
    }
    # Both strategy exit paths deliberately consume only filled/open rows.  A
    # symbol-wide pending label must not hide component A from either planner.
    assert _open_long_trades(projected)["trade_id"].to_list() == ["trade-a"]
    assert _open_continuous_trades(projected, "strategy")["trade_id"].to_list() == [
        "trade-a"
    ]


def test_entry_fill_anchor_and_resize_preserve_original_lifecycle_clock(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    opened = _submit(
        kernel,
        batch="open",
        qty=2.0,
        reason="entry",
        metadata={"max_hold_hours": 24},
    )

    pending = canonical_strategy_trade_rows(root, sleeve="long")
    assert pending.select(
        "entry_target_ts_ms",
        "entry_ts_ms",
        "max_hold_deadline_ts_ms",
        "account_lifecycle_attribution_status",
    ).to_dicts() == [{
        "entry_target_ts_ms": 1_000,
        "entry_ts_ms": None,
        "max_hold_deadline_ts_ms": None,
        "account_lifecycle_attribution_status": "pending_first_fill",
    }]

    _ack_and_fill(
        kernel,
        opened,
        fills=[("entry-fill", 2.0, 10.5, 2_000_000_000)],
    )
    clock.advance_to_wall_ns(3_000_000_000)
    resized = _submit(
        kernel,
        batch="resize",
        qty=3.0,
        reason="resize",
        reference_price=12.0,
        metadata={"max_hold_hours": 1},
    )
    _ack_and_fill(
        kernel,
        resized,
        fills=[("resize-fill", 1.0, 12.0, 4_000_000_000)],
    )

    anchor = canonical_component_execution_anchors(root, sleeve="long")[0]
    assert anchor.entry_target_batch_id == "open"
    assert anchor.entry_fill_execution_id == "entry-fill"
    assert anchor.entry_fill_ts_ms == 2_000
    assert anchor.entry_fill_vwap == pytest.approx(10.5)

    row = canonical_strategy_trade_rows(root, sleeve="long").to_dicts()[0]
    assert row["entry_target_ts_ms"] == 1_000
    assert row["target_updated_ts_ms"] == 3_000
    assert row["entry_ts_ms"] == 2_000
    assert row["entry_price"] == pytest.approx(10.5)
    assert row["max_hold_duration_ms"] == 24 * 60 * 60 * 1_000
    assert row["max_hold_duration_basis"] == "metadata_max_hold_hours"
    assert row["max_hold_deadline_ts_ms"] == 2_000 + 24 * 60 * 60 * 1_000


def test_same_direction_group_exposes_only_group_fill_scope(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    keys = (
        "long/strategy/trade-a/BUSDT",
        "long/strategy/trade-b/BUSDT",
    )
    opened = _submit_group(
        kernel,
        batch="group-entry",
        targets=[(keys[0], "trade-a", 1.0), (keys[1], "trade-b", 2.0)],
        reason="entry",
        metadata={"max_hold_duration_ms": 3_600_000},
    )
    assert len(opened.commands) == 1
    _ack_and_fill(
        kernel,
        opened,
        fills=[
            ("group-fill-1", 1.0, 10.0, 2_000_000_000),
            ("group-fill-2", 2.0, 11.0, 3_000_000_000),
        ],
    )

    anchors = canonical_component_execution_anchors(root, sleeve="long")
    assert len(anchors) == 2
    for anchor in anchors:
        assert anchor.entry_fill_ts_ms == 2_000
        assert anchor.entry_first_fill_price == pytest.approx(10.0)
        assert anchor.entry_fill_vwap == pytest.approx(32.0 / 3.0)
        assert anchor.entry_observed_signed_qty == pytest.approx(3.0)
        assert anchor.entry_fill_complete
        assert anchor.entry_attribution_scope == "same_direction_new_entry_group"
        assert anchor.entry_attribution_basis == "same_direction_entry_group_first_fill"
        assert anchor.entry_attribution_status == "group_fill_attributed"
        assert anchor.entry_group_target_keys == keys

    rows = canonical_strategy_trade_rows(root, sleeve="long").sort("trade_id")
    assert rows["status"].to_list() == ["open", "open"]
    assert rows["entry_ts_ms"].to_list() == [2_000, 2_000]
    assert rows["entry_price"].to_list() == pytest.approx([32.0 / 3.0] * 2)
    assert rows["entry_attribution_scope"].to_list() == [
        "same_direction_new_entry_group",
        "same_direction_new_entry_group",
    ]
    # Repeated values are explicitly group execution facts, not component P&L.
    assert "net_pnl_usdt" not in rows.columns


def test_mixed_direction_netted_batch_has_null_lifecycle_clocks(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    opened = _submit_group(
        kernel,
        batch="ambiguous-net-entry",
        targets=[
            ("long/strategy/trade-long/BUSDT", "trade-long", 2.0),
            ("long/strategy/trade-short/BUSDT", "trade-short", -1.0),
        ],
        reason="entry",
        metadata={"max_hold_duration_ms": 3_600_000},
    )
    assert opened.commands[0].signed_qty == pytest.approx(1.0)
    _ack_and_fill(
        kernel,
        opened,
        fills=[("net-fill", 1.0, 10.0, 2_000_000_000)],
    )

    anchors = canonical_component_execution_anchors(root, sleeve="long")
    assert len(anchors) == 2
    assert {anchor.entry_attribution_status for anchor in anchors} == {
        "ambiguous_aggregate_only"
    }
    assert all(anchor.entry_fill_ts_ms is None for anchor in anchors)
    assert all(anchor.entry_fill_vwap is None for anchor in anchors)

    rows = canonical_strategy_trade_rows(root, sleeve="long").sort("trade_id")
    assert rows["account_target_status"].to_list() == ["converged", "converged"]
    assert rows["status"].to_list() == ["target_pending", "target_pending"]
    assert rows["target_action"].to_list() == [
        "attribution_review",
        "attribution_review",
    ]
    assert rows["entry_ts_ms"].to_list() == [None, None]
    assert rows["max_hold_deadline_ts_ms"].to_list() == [None, None]
    assert rows["account_lifecycle_attribution_status"].to_list() == [
        "ambiguous_aggregate_only",
        "ambiguous_aggregate_only",
    ]


def test_terminal_group_reduction_projects_one_account_pnl_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    keys = (
        "long/strategy/trade-a/BUSDT",
        "long/strategy/trade-b/BUSDT",
    )
    opened = _submit_group(
        kernel,
        batch="group-open",
        targets=[(keys[0], "trade-a", 1.0), (keys[1], "trade-b", 2.0)],
        reason="entry",
    )
    _ack_and_fill(
        kernel,
        opened,
        fills=[
            ("open-fill-1", 1.0, 10.0, 2_000_000_000),
            ("open-fill-2", 2.0, 11.0, 3_000_000_000),
        ],
    )

    clock.advance_to_wall_ns(4_000_000_000)
    closed = _submit_group(
        kernel,
        batch="group-close",
        targets=[(keys[0], "trade-a", 0.0), (keys[1], "trade-b", 0.0)],
        reason="max_hold",
        reference_price=9.0,
    )
    _ack_and_fill(
        kernel,
        closed,
        fills=[("close-fill", -3.0, 9.0, 5_000_000_000)],
    )
    close_command = closed.commands[0]
    kernel.finalize_flat_position(
        symbol="BUSDT",
        command_id=close_command.command_id,
        exchange_ts_ns=4_999_999_999,
        local_receive_ts_ns=5_000_000_000,
        reason="max_hold",
    )

    reductions = canonical_reduction_events(
        root,
        sleeve="long",
        strategy_ids=("strategy",),
    )
    assert len(reductions) == 1
    reduction = reductions[0]
    assert reduction.accounting_scope == "symbol_reduce_batch"
    assert reduction.batch_id == "group-close"
    assert reduction.all_component_target_keys == keys
    assert reduction.matched_component_target_keys == keys
    assert reduction.component_attribution_status == "pending_account_netting"
    assert reduction.net_pnl_usdt < 0.0
    assert reduction.fee_status == "observed_execution_fee"
    assert reduction.funding_status == "pending_venue_reconciliation"
    assert reduction.adverse
    assert reduction.adverse_basis == "negative_provisional_net_pending_funding"
    adverse = canonical_adverse_reduction_events(root, sleeve="long")
    assert [event.pnl_key for event in adverse] == [reduction.pnl_key]
    event_snapshot = read_account_journal(root, verify=True)
    shared_projection = canonical_account_projection(
        root,
        account_events=event_snapshot,
        trusted_account_state=kernel._state_ref(),
    )
    replayed_projection = canonical_account_projection(
        root,
        account_events=event_snapshot,
    )
    assert shared_projection.accepted_batches == replayed_projection.accepted_batches
    assert shared_projection.quantity_tolerance == replayed_projection.quantity_tolerance
    assert shared_projection.component_revisions == replayed_projection.component_revisions
    assert shared_projection.execution_anchors == replayed_projection.execution_anchors
    with pytest.raises(RuntimeError, match="does not match"):
        canonical_account_projection(
            root,
            account_events=event_snapshot,
            trusted_account_state=AccountState(),
        )
    indexed_anchors = canonical_component_execution_anchors(
        root,
        sleeve="long",
        account_events=event_snapshot,
    )
    assert indexed_anchors == canonical_component_execution_anchors(
        root,
        sleeve="long",
        account_projection=shared_projection,
    )
    assert indexed_anchors == canonical_component_execution_anchors(
        root,
        sleeve="long",
    )
    import liquidity_migration.account_strategy_state as strategy_state_module

    with monkeypatch.context() as reference:
        reference.setattr(
            strategy_state_module,
            "_build_batch_fill_index",
            lambda _events, *, state: None,
        )
        scan_reference_anchors = canonical_component_execution_anchors(
            root,
            sleeve="long",
            account_events=event_snapshot,
        )
    assert indexed_anchors == scan_reference_anchors
    assert canonical_reduction_events(
        root,
        sleeve="long",
        account_events=event_snapshot,
    ) == reductions
    assert canonical_adverse_reduction_events(
        root,
        sleeve="long",
        account_events=event_snapshot,
    ) == adverse
    assert sum(event.net_pnl_usdt for event in reductions) == pytest.approx(
        reduction.net_pnl_usdt
    )

    rows = canonical_strategy_trade_rows(root, sleeve="long").sort("trade_id")
    shared_rows = canonical_strategy_trade_rows(
        root,
        sleeve="long",
        account_projection=shared_projection,
    ).sort("trade_id")
    assert shared_rows.equals(rows)
    assert rows["status"].to_list() == ["closed", "closed"]
    assert rows["exit_target_ts_ms"].to_list() == [4_000, 4_000]
    assert rows["exit_ts_ms"].to_list() == [5_000, 5_000]
    assert rows["cooldown_start_ts_ms"].to_list() == [5_000, 5_000]
    assert rows["exit_attribution_scope"].to_list() == [
        "same_direction_reduction_group",
        "same_direction_reduction_group",
    ]
    assert rows["exit_pnl_key"].to_list() == [reduction.pnl_key] * 2
    assert "net_pnl_usdt" not in rows.columns


def test_partial_entry_fill_opens_lifecycle_without_claiming_target_convergence(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    opened = _submit(
        kernel,
        batch="partial-entry",
        qty=2.0,
        reason="entry",
        metadata={"max_hold_duration_ms": 3_600_000},
    )
    _ack_and_fill(
        kernel,
        opened,
        fills=[("partial-fill", 1.0, 10.25, 2_000_000_000)],
    )

    anchor = canonical_component_execution_anchors(root, sleeve="long")[0]
    assert anchor.entry_fill_ts_ms == 2_000
    assert anchor.entry_fill_vwap == pytest.approx(10.25)
    assert anchor.entry_observed_signed_qty == pytest.approx(1.0)
    assert not anchor.entry_fill_complete

    row = canonical_strategy_trade_rows(root, sleeve="long").to_dicts()[0]
    assert row["status"] == "open"
    assert row["account_execution_lifecycle_status"] == "open"
    assert row["account_target_status"] == "pending"
    assert row["target_action"] == "open_or_resize"
    assert row["entry_ts_ms"] == 2_000
    assert row["max_hold_deadline_ts_ms"] == 2_000 + 3_600_000


def test_positive_reduction_is_not_an_adverse_event(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    opened = _submit(kernel, batch="open", qty=2.0, reason="entry")
    _ack_and_fill(
        kernel,
        opened,
        fills=[("open-fill", 2.0, 10.0, 2_000_000_000)],
    )
    clock.advance_to_wall_ns(3_000_000_000)
    closed = _submit(
        kernel,
        batch="take-profit",
        qty=0.0,
        reason="take_profit",
        reference_price=12.0,
    )
    _ack_and_fill(
        kernel,
        closed,
        fills=[("close-fill", -2.0, 12.0, 4_000_000_000)],
    )
    kernel.finalize_flat_position(
        symbol="BUSDT",
        command_id=closed.commands[0].command_id,
        exchange_ts_ns=3_999_999_999,
        local_receive_ts_ns=4_000_000_000,
        reason="take_profit",
    )

    reductions = canonical_reduction_events(root, sleeve="long")
    assert len(reductions) == 1
    assert reductions[0].net_pnl_usdt > 0.0
    assert not reductions[0].adverse
    assert reductions[0].adverse_basis == "nonnegative_net_pnl"
    assert canonical_adverse_reduction_events(root, sleeve="long") == ()


def test_native_stop_reduction_retains_pending_fee_and_funding_provenance(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    root = tmp_path / "account"
    kernel = AccountExecutionKernel(root, account_id="a", clock=clock)
    opened = _submit(kernel, batch="open", qty=2.0, reason="entry")
    _ack_and_fill(
        kernel,
        opened,
        fills=[("open-fill", 2.0, 10.0, 2_000_000_000)],
    )
    kernel.record_protection(
        protection_key="native-stop",
        symbol="BUSDT",
        status="active",
        stop_price=8.5,
        take_profit_price=None,
        exchange_ts_ns=2_500_000_000,
        local_receive_ts_ns=2_500_000_001,
        metadata={"native_exchange": True, "symbol": "BUSDT"},
    )
    kernel.adopt_external_protection_fill(
        protection_key="native-stop",
        venue_order_id="native-order",
        execution_id="native-fill",
        symbol="BUSDT",
        signed_qty=-2.0,
        price=8.0,
        fee_usdt=0.0,
        exchange_ts_ns=3_000_000_000,
        local_receive_ts_ns=3_000_000_001,
        reason="native_protection_triggered",
        execution_origin="verified_native_stop",
        metadata={
            "fee_observed": False,
            "fee_status": "pending_missing_execution_fee",
        },
    )

    adverse = canonical_adverse_reduction_events(root, sleeve="long")
    assert len(adverse) == 1
    event = adverse[0]
    assert event.component_reasons == ("native_protection_triggered",)
    assert event.matched_component_target_keys == (
        "long/strategy/trade-1/BUSDT",
    )
    assert event.fee_status == "pending_missing_or_unknown_execution_fee"
    assert event.funding_status == "pending_venue_reconciliation"
    assert event.adverse_basis == "negative_provisional_net_pending_fees_and_funding"

    row = canonical_strategy_trade_rows(root, sleeve="long").to_dicts()[0]
    assert row["status"] == "closed"
    assert row["exit_ts_ms"] == 3_000
    assert row["cooldown_start_ts_ms"] == 3_000
    assert row["exit_reason"] == "native_protection_triggered"
