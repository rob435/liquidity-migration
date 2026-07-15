from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
    OrderCommand,
)
from liquidity_migration.deterministic_runtime import DeterministicIds, VirtualClock, VirtualScheduler
from liquidity_migration.execution_adapters import (
    BookLevel,
    ExecutionObservationType,
    ExecutionTwinConfig,
    L2BookSnapshot,
    LatencyProfile,
    MarketOrderExecutionTwin,
)
from liquidity_migration.execution_twin_account import (
    ExecutionTwinAccount,
    ProtectionActivationQueue,
    TwinAccountConfig,
    forced_flatten_intents,
    protection_trigger_reason,
)
from liquidity_migration.strategy_runtime import (
    AccountKernelRuntime,
    AdaptedIntent,
    LongTargetAdapter,
    SleeveTargetIntent,
)


def _rules() -> dict[str, InstrumentRules]:
    return {
        "BUSDT": InstrumentRules(
            symbol="BUSDT",
            qty_step=0.1,
            min_qty=0.1,
            min_notional=1.0,
            max_order_qty=100.0,
            max_leverage=20.0,
        )
    }


def _policy() -> AccountRiskPolicy:
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=1_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=100.0,
        max_leverage=10.0,
    )


def _snapshot() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(100.0, 100.0, "wallet", 900_000_000)


def _book() -> L2BookSnapshot:
    return L2BookSnapshot(
        symbol="BUSDT",
        sequence=1,
        previous_sequence=0,
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        bids=(BookLevel(9.9, 100.0),),
        asks=(BookLevel(10.1, 100.0),),
    )


def _adapter() -> MarketOrderExecutionTwin:
    return MarketOrderExecutionTwin(
        books={"BUSDT": _book()},
        instrument_rules=_rules(),
        config=ExecutionTwinConfig(
            fee_bps=5.5,
            latency=LatencyProfile(1, 1, 1),
            max_decision_age_ns=200_000_000,
        ),
    )


@pytest.mark.parametrize(
    ("signed_qty", "side", "visible_price", "expected_price"),
    [
        (1.0, "Buy", 10.1, 10.1101),
        (-1.0, "Sell", 9.9, 9.8901),
    ],
)
def test_twin_applies_calibrated_residual_slippage_after_visible_book_walk(
    signed_qty: float,
    side: str,
    visible_price: float,
    expected_price: float,
) -> None:
    adapter = MarketOrderExecutionTwin(
        books={"BUSDT": _book()},
        instrument_rules=_rules(),
        config=ExecutionTwinConfig(
            fee_bps=5.5,
            latency=LatencyProfile(1, 1, 1),
            max_decision_age_ns=100,
            residual_adverse_slippage_bps=10.0,
        ),
    )
    command = OrderCommand(
        command_id=f"residual-{side.lower()}",
        batch_id="batch-residual",
        symbol="BUSDT",
        side=side,
        qty=1.0,
        signed_qty=signed_qty,
        reduce_only=False,
        reference_price=10.0,
        target_signed_qty=signed_qty,
        chunk_index=1,
        chunk_count=1,
        created_ts_ns=1_000_000_000,
    )

    observations = tuple(adapter.submit(command, _book().market_ref(input_key="book")))
    fill = next(
        item
        for item in observations
        if item.observation_type == ExecutionObservationType.FILL
    )
    assert fill.metadata["visible_book_price"] == visible_price
    assert fill.price == pytest.approx(expected_price)


def test_uncalibrated_partial_fill_policy_rejects_multi_level_path() -> None:
    book = L2BookSnapshot(
        symbol="BUSDT",
        sequence=1,
        previous_sequence=0,
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        bids=(BookLevel(9.9, 100.0),),
        asks=(BookLevel(10.1, 0.6), BookLevel(10.2, 0.6)),
    )
    adapter = MarketOrderExecutionTwin(
        books={"BUSDT": book},
        instrument_rules=_rules(),
        config=ExecutionTwinConfig(
            fee_bps=5.5,
            latency=LatencyProfile(1, 1, 1, fill_spacing_ns=0),
            max_decision_age_ns=100,
            allow_partial_fills=False,
            fill_partition_policy="single_level_full_fill_or_reject",
        ),
    )
    command = OrderCommand(
        command_id="uncalibrated-split",
        batch_id="batch-uncalibrated-split",
        symbol="BUSDT",
        side="Buy",
        qty=1.0,
        signed_qty=1.0,
        reduce_only=False,
        reference_price=10.0,
        target_signed_qty=1.0,
        chunk_index=1,
        chunk_count=1,
        created_ts_ns=1_000_000_000,
    )

    observations = tuple(adapter.submit(command, book.market_ref(input_key="book")))
    assert len(observations) == 1
    assert observations[0].observation_type == ExecutionObservationType.ACK
    assert observations[0].accepted is False
    assert observations[0].metadata["reason"] == "unidentified_split_fill_path"
    assert observations[0].metadata["fill_partition_policy"] == (
        "single_level_full_fill_or_reject"
    )


def test_twin_anchors_to_command_time_and_separates_first_fill_from_spacing() -> None:
    book = L2BookSnapshot(
        symbol="BUSDT",
        sequence=2,
        previous_sequence=1,
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        bids=(BookLevel(9.9, 10.0),),
        asks=(BookLevel(10.1, 0.5), BookLevel(10.2, 0.5)),
    )
    config = ExecutionTwinConfig(
        fee_bps=5.5,
        latency=LatencyProfile(
            decision_to_socket_ns=10,
            order_entry_ns=20,
            order_response_ns=30,
            submit_to_first_fill_ns=40,
            fill_response_ns=50,
            fill_spacing_ns=7,
        ),
        max_decision_age_ns=200,
    )
    command = OrderCommand(
        command_id="timed-command",
        batch_id="timed-batch",
        symbol="BUSDT",
        side="Buy",
        qty=1.0,
        signed_qty=1.0,
        reduce_only=False,
        reference_price=10.0,
        target_signed_qty=1.0,
        chunk_index=0,
        chunk_count=1,
        created_ts_ns=1_000_000_100,
    )

    observations = tuple(
        MarketOrderExecutionTwin(
            books={"BUSDT": book},
            instrument_rules=_rules(),
            config=config,
        ).submit(command, book.market_ref(input_key="timed-book"))
    )
    ack = next(item for item in observations if item.observation_type == ExecutionObservationType.ACK)
    fills = [item for item in observations if item.observation_type == ExecutionObservationType.FILL]
    status = next(
        item for item in observations if item.observation_type == ExecutionObservationType.ORDER_STATUS
    )
    assert ack.metadata["local_socket_send_ts_ns"] == command.created_ts_ns + 10
    assert ack.metadata["decision_book_age_ns"] == 110
    assert ack.exchange_ts_ns == command.created_ts_ns + 30
    assert [item.exchange_ts_ns for item in fills] == [
        command.created_ts_ns + 50,
        command.created_ts_ns + 57,
    ]
    assert [item.local_receive_ts_ns for item in fills] == [
        command.created_ts_ns + 100,
        command.created_ts_ns + 107,
    ]
    assert status.exchange_ts_ns == fills[-1].exchange_ts_ns
    assert status.local_receive_ts_ns == fills[-1].local_receive_ts_ns

    stale = MarketOrderExecutionTwin(
        books={"BUSDT": book},
        instrument_rules=_rules(),
        config=ExecutionTwinConfig(
            fee_bps=5.5,
            latency=config.latency,
            max_decision_age_ns=109,
        ),
    )
    rejection = tuple(stale.submit(command, book.market_ref(input_key="timed-book")))
    assert len(rejection) == 1
    assert rejection[0].metadata["reason"] == "stale_decision"

    # A decision cannot consume a book that arrived after the command, even if
    # configured decision-to-socket delay would move the modeled send later.
    future_book_command = replace(command, created_ts_ns=book.local_receive_ts_ns - 5)
    future = tuple(
        MarketOrderExecutionTwin(
            books={"BUSDT": book},
            instrument_rules=_rules(),
            config=config,
        ).submit(future_book_command, book.market_ref(input_key="timed-book"))
    )
    assert len(future) == 1
    assert future[0].metadata["reason"] == "future_decision_book"


def _open_long(root: Path, clock: VirtualClock) -> tuple[AccountExecutionKernel, str]:
    kernel = AccountExecutionKernel(root, account_id="twin-account", clock=clock, id_seed="twin-test")
    market = _book().market_ref(input_key="book-1")
    result = AccountKernelRuntime(kernel).process_cycle(
        batch_id="open",
        intents=[AdaptedIntent(LongTargetAdapter(), SleeveTargetIntent(
            decision_key="long-open",
            target_key="long/main/BUSDT",
            strategy_id="long-v1",
            component_id="main",
            symbol="BUSDT",
            signed_notional_usdt=20.0,
            leverage=10.0,
            reason="entry",
        ))],
        market_inputs={"BUSDT": market},
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
        execution_adapter=_adapter(),
    )
    return kernel, result.target_result.commands[0].command_id


def test_funding_wallet_and_liquidation_are_account_level(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_100_000_000, current_monotonic_ns=100)
    kernel, _ = _open_long(tmp_path, clock)
    model = ExecutionTwinAccount(TwinAccountConfig(
        starting_wallet_balance_usdt=10.0,
        maintenance_margin_rate=0.05,
        liquidation_fee_bps=50.0,
        protection_activation_delay_ns=10,
    ))
    model.record_funding(
        kernel,
        funding_key="funding-1",
        funding_rates={"BUSDT": 0.001},
        mark_prices={"BUSDT": 10.0},
        exchange_ts_ns=1_200_000_000,
        local_receive_ts_ns=1_205_000_000,
    )
    # A funding cash-flow is not a fill-accounting checkpoint. Advancing the
    # fee/realized checkpoint here would make a later close silently omit the
    # entry fee from its account P&L row.
    assert kernel.state().positions["BUSDT"].reported_realized_usdt == 0.0
    assert kernel.state().positions["BUSDT"].reported_fees_usdt == 0.0
    # Long pays positive funding: 2 * $10 * 0.1% = $0.02.
    assert model.wallet_balance(kernel.state()) == pytest.approx(10.0 - 0.02 - 20.2 * 5.5 / 10_000)
    safe = model.value(kernel.state(), mark_prices={"BUSDT": 10.0})
    assert not safe.liquidation_required
    liquidating = model.value(kernel.state(), mark_prices={"BUSDT": 0.01})
    assert liquidating.liquidation_required
    assert forced_flatten_intents(kernel.state(), decision_prefix="liq", reason="liquidation")


def test_protection_activation_delay_is_virtual_and_replayable(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_100_000_000, current_monotonic_ns=100)
    kernel, command_id = _open_long(tmp_path, clock)
    scheduler = VirtualScheduler(clock=clock, ids=DeterministicIds("protection"))
    queue = ProtectionActivationQueue(scheduler, delay_ns=50)
    queue.request(
        protection_key="BUSDT-p1",
        symbol="BUSDT",
        command_id=command_id,
        stop_price=9.0,
        take_profit_price=12.0,
    )
    assert queue.activate_due(kernel) == ()
    clock.advance_ns(49)
    assert queue.activate_due(kernel) == ()
    clock.advance_ns(1)
    events = queue.activate_due(kernel)
    assert len(events) == 1
    assert kernel.state().protections["BUSDT-p1"]["status"] == "active"
    assert kernel.state().protections["BUSDT-p1"]["metadata"]["activation_delay_ns"] == 50


@pytest.mark.parametrize(
    ("qty", "mark", "stop", "tp", "expected"),
    [
        (1.0, 8.9, 9.0, 12.0, "stop_loss"),
        (1.0, 12.1, 9.0, 12.0, "take_profit"),
        (-1.0, 11.1, 11.0, 8.0, "stop_loss"),
        (-1.0, 7.9, 11.0, 8.0, "take_profit"),
        (1.0, 10.0, 9.0, 12.0, ""),
    ],
)
def test_protection_trigger_direction(qty: float, mark: float, stop: float, tp: float, expected: str) -> None:
    assert protection_trigger_reason(
        signed_qty=qty,
        mark_price=mark,
        stop_price=stop,
        take_profit_price=tp,
    ) == expected
