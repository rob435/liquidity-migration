from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path

import pytest

import liquidity_migration.account.account_kernel as account_kernel_module
from liquidity_migration.account.account_contracts import (
    AccountState,
    OrderState,
    transaction_state_copy,
)
from liquidity_migration.account.account_kernel import (
    AccountEvent,
    AccountEventType,
    AccountExecutionKernel,
    AccountKernelError,
    AccountJournalIntegrityError,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
    OrderCommand,
    account_journal_path,
    account_transactions_path,
    quantity_tolerance,
    read_account_journal,
    read_account_journal_head,
    verify_account_journal,
)
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.marketdata.bybit_errors import BybitRequestRejected, BybitSubmissionUncertain
from liquidity_migration.venue.bybit_execution_adapter import BybitDemoExecutionAdapter
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.execution_adapters import (
    AmbiguousExposureSubmission,
    BookLevel,
    ExecutionObservation,
    ExecutionTwinConfig,
    KernelExecutionDriver,
    L2BookSnapshot,
    LatencyProfile,
    MarketOrderExecutionTwin,
    StaleUnsubmittedExposureCommand,
)
from liquidity_migration.account.strategy_runtime import (
    AccountKernelRuntime,
    AdaptedIntent,
    ContinuousTargetAdapter,
    HedgeTargetAdapter,
    LongTargetAdapter,
    RiskTargetAdapter,
    SleeveTargetIntent,
    set_producer_price_max_age_ns,
)


def _market(*, price: float = 10.0, key: str = "book-1") -> MarketInputRef:
    return MarketInputRef(
        input_key=key,
        symbol="BUSDT",
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        reference_price=price,
        bid_price=price - 0.01,
        ask_price=price + 0.01,
        book_sequence=42,
        source="test_l2",
    )


def _target(
    *,
    decision: str,
    key: str,
    sleeve: str,
    qty: float,
    price: float = 10.0,
    leverage: float = 10.0,
    metadata: dict[str, object] | None = None,
) -> DesiredTarget:
    return DesiredTarget(
        decision_key=decision,
        target_key=key,
        sleeve=sleeve,
        strategy_id=f"{sleeve}-strategy",
        component_id=f"{sleeve}-component",
        symbol="BUSDT",
        signed_qty=qty,
        reference_price=price,
        leverage=leverage,
        reason="test target",
        metadata=metadata or {},
    )


def _rules(
    *,
    min_notional: float = 1.0,
    max_order_qty: float = 100.0,
    tick_size: float = 0.0,
) -> dict[str, InstrumentRules]:
    return {
        "BUSDT": InstrumentRules(
            symbol="BUSDT",
            qty_step=0.1,
            min_qty=0.1,
            min_notional=min_notional,
            tick_size=tick_size,
            max_order_qty=max_order_qty,
            max_leverage=20.0,
        )
    }


def _snapshot() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        equity_usdt=10_000.0,
        available_margin_usdt=9_000.0,
        snapshot_key="wallet-1",
        snapshot_ts_ns=950_000_000,
    )


def _policy() -> AccountRiskPolicy:
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=500.0,
        max_symbol_notional_usdt=500.0,
        max_initial_margin_usdt=100.0,
        max_leverage=10.0,
    )


def _kernel(root: Path, *, account_id: str = "bybit-demo-test") -> AccountExecutionKernel:
    return AccountExecutionKernel(
        root,
        account_id=account_id,
        clock=VirtualClock(current_wall_ns=1_100_000_000, current_monotonic_ns=100_000_000),
        id_seed="test-seed",
    )


def _book(*, bid_qty: float = 10.0, ask_qty: float = 10.0, gap: bool = False) -> L2BookSnapshot:
    return L2BookSnapshot(
        symbol="BUSDT",
        sequence=100,
        previous_sequence=99,
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        bids=(BookLevel(9.9, bid_qty), BookLevel(9.8, bid_qty)),
        asks=(BookLevel(10.1, ask_qty), BookLevel(10.2, ask_qty)),
        sequence_gap=gap,
        clock_offset_estimate_ns=100_000_000,
    )


def _twin(
    *,
    name: str,
    book: L2BookSnapshot | None = None,
    rules: dict[str, InstrumentRules] | None = None,
) -> MarketOrderExecutionTwin:
    return MarketOrderExecutionTwin(
        name=name,
        books={"BUSDT": book or _book()},
        instrument_rules=rules or _rules(),
        config=ExecutionTwinConfig(
            fee_bps=5.5,
            latency=LatencyProfile(
                decision_to_socket_ns=10_000_000,
                order_entry_ns=2_000_000,
                order_response_ns=3_000_000,
            ),
            # Command creation is 100 ms after the captured book and the
            # modeled socket send is another 10 ms later.
            max_decision_age_ns=200_000_000,
        ),
    )


def test_account_batch_nets_all_same_symbol_components_before_one_command(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    result = kernel.submit_targets(
        batch_id="batch-1",
        market_inputs=[_market()],
        targets=[
            _target(decision="continuous-1", key="continuous/main/BUSDT", sleeve="continuous", qty=-5.0),
            _target(decision="hedge-1", key="hedge/main/BUSDT", sleeve="hedge", qty=1.0),
            _target(decision="long-1", key="long/main/BUSDT", sleeve="long", qty=2.0),
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )

    assert result.accepted
    assert result.rejection_keys == ()
    assert len(result.commands) == 1
    command = result.commands[0]
    assert command.symbol == "BUSDT"
    assert command.side == "Sell"
    assert command.signed_qty == pytest.approx(-2.0)
    assert command.leverage == 10.0
    assert not command.reduce_only

    events = read_account_journal(tmp_path)
    assert [event.event_type for event in events] == [
        AccountEventType.MARKET_INPUT_REF.value,
        AccountEventType.DECISION.value,
        AccountEventType.DECISION.value,
        AccountEventType.DECISION.value,
        AccountEventType.TARGET.value,
        AccountEventType.TARGET.value,
        AccountEventType.TARGET.value,
        AccountEventType.RISK_DECISION.value,
        AccountEventType.ORDER_COMMAND.value,
    ]
    risk = next(event for event in events if event.event_type == AccountEventType.RISK_DECISION.value)
    assert risk.payload["component_gross_notional_usdt"] == pytest.approx(80.0)
    assert risk.payload["account_gross_notional_usdt"] == pytest.approx(20.0)
    assert risk.payload["component_initial_margin_usdt"] == pytest.approx(8.0)
    assert risk.payload["aggregate_targets"] == {"BUSDT": -2.0}


def test_sign_flip_guard_compares_each_quantity_to_tolerance_not_their_product(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    policy = AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=1_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=1_000.0,
        max_leverage=10.0,
        quantity_tolerance=1e-6,
    )
    rules = {
        "BUSDT": InstrumentRules(
            symbol="BUSDT",
            qty_step=1e-7,
            min_qty=1e-7,
            min_notional=0.0,
            max_order_qty=100.0,
            max_leverage=20.0,
        )
    }
    opened = kernel.submit_targets(
        batch_id="tiny-open",
        market_inputs=[_market()],
        targets=[
            _target(
                decision="tiny-open",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=2e-4,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=policy,
        instrument_rules=rules,
    )
    assert opened.accepted
    command = opened.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-tiny-open",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_201_000_000,
    )
    kernel.record_fill(
        command_id=command.command_id,
        execution_id="fill-tiny-open",
        signed_qty=2e-4,
        price=10.0,
        fee_usdt=0.0,
        exchange_ts_ns=1_202_000_000,
        local_receive_ts_ns=1_203_000_000,
    )

    flipped = kernel.submit_targets(
        batch_id="tiny-direct-flip",
        market_inputs=[_market(key="book-tiny-flip")],
        targets=[
            _target(
                decision="tiny-direct-flip",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=-2e-4,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=policy,
        instrument_rules=rules,
    )

    assert not flipped.accepted
    assert flipped.rejection_keys == ("account-risk:tiny-direct-flip:sign_flip_requires_flat:BUSDT",)
    assert flipped.commands == ()


def test_risk_rejection_is_atomic_and_does_not_commit_targets(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    tight = AccountRiskPolicy(
        max_component_gross_notional_usdt=10.0,
        max_account_gross_notional_usdt=10.0,
        max_symbol_notional_usdt=10.0,
        max_initial_margin_usdt=1.0,
        max_leverage=10.0,
    )
    result = kernel.submit_targets(
        batch_id="rejected-batch",
        market_inputs=[_market()],
        targets=[_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=-5.0)],
        risk_snapshot=_snapshot(),
        risk_policy=tight,
        instrument_rules=_rules(),
    )

    assert not result.accepted
    assert result.commands == ()
    assert "account-risk:rejected-batch:component_gross_limit" in result.rejection_keys
    state = kernel.state()
    assert state.component_targets == {}
    assert state.aggregate_targets == {}
    assert state.orders == {}


def test_only_venue_minimum_applies_not_a_hidden_25_dollar_floor(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    accepted = kernel.submit_targets(
        batch_id="one-dollar-order",
        market_inputs=[_market()],
        targets=[_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=0.1)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(min_notional=1.0),
    )
    assert accepted.accepted
    assert accepted.commands[0].qty == pytest.approx(0.1)

    other = _kernel(tmp_path / "below")
    rejected = other.submit_targets(
        batch_id="below-venue-minimum",
        market_inputs=[_market(price=9.0)],
        targets=[
            _target(
                decision="d2",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=0.1,
                price=9.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(min_notional=1.0),
    )
    assert not rejected.accepted
    assert rejected.rejection_keys == ("account-risk:below-venue-minimum:below_min_notional:BUSDT",)


def test_partial_fills_survive_restart_and_have_replayable_position_hash(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    result = kernel.submit_targets(
        batch_id="partial-fill",
        market_inputs=[_market()],
        targets=[_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=-2.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-1",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_210_000_000,
    )
    kernel.record_fill(
        command_id=command.command_id,
        execution_id="exec-1",
        signed_qty=-0.5,
        price=10.1,
        fee_usdt=0.001,
        exchange_ts_ns=1_220_000_000,
        local_receive_ts_ns=1_225_000_000,
    )

    restarted = _kernel(tmp_path)
    partial = restarted.state()
    assert partial.orders[command.command_id].status == "partially_filled"
    assert partial.positions["BUSDT"].signed_qty == pytest.approx(-0.5)
    partial_hash = read_account_journal(tmp_path)[-1].state_hash
    assert partial.state_hash() == partial_hash

    restarted.record_fill(
        command_id=command.command_id,
        execution_id="exec-2",
        signed_qty=-1.5,
        price=10.2,
        fee_usdt=0.003,
        exchange_ts_ns=1_230_000_000,
        local_receive_ts_ns=1_235_000_000,
    )
    final = restarted.state()
    assert final.orders[command.command_id].status == "filled"
    assert final.positions["BUSDT"].signed_qty == pytest.approx(-2.0)
    assert final.positions["BUSDT"].average_price == pytest.approx(10.175)


def test_racing_ack_observers_are_semantically_idempotent_under_journal_lock(
    tmp_path: Path,
) -> None:
    first = _kernel(tmp_path)
    result = first.submit_targets(
        batch_id="racing-ack",
        market_inputs=[_market()],
        targets=[_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=2.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    command = result.commands[0]
    stale_observer = _kernel(tmp_path)
    assert stale_observer.state().orders[command.command_id].status == "commanded"

    committed = first.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-race",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_210_000_000,
        metadata={"source": "bybit_create_response"},
    )
    duplicate = stale_observer.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-race",
        exchange_ts_ns=1_205_000_000,
        local_ack_ts_ns=1_215_000_000,
        metadata={"source": "bybit_private_execution_ws"},
    )

    assert len(committed) == 1
    assert duplicate == ()
    assert [event.event_type for event in read_account_journal(tmp_path)].count(AccountEventType.ACK.value) == 1
    assert stale_observer.state().orders[command.command_id].ack_accepted is True


def test_late_http_ack_timing_is_supplemental_to_an_inferred_semantic_ack(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    result = kernel.submit_targets(
        batch_id="late-http-ack",
        market_inputs=[_market()],
        targets=[
            _target(
                decision="d1",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=2.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-late-http",
        exchange_ts_ns=1_205_000_000,
        local_ack_ts_ns=1_215_000_000,
        metadata={"inferred_from_execution_id": "execution-1"},
    )

    timing = _kernel(tmp_path).record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-late-http",
        exchange_ts_ns=1_203_000_000,
        local_ack_ts_ns=1_216_000_000,
        metadata={
            "local_socket_send_ts_ns": 1_201_000_000,
            "source": "bybit_create_response",
        },
    )

    assert len(timing) == 1
    assert timing[0].event_type == AccountEventType.ACK_OBSERVATION.value
    events = read_account_journal(tmp_path)
    assert sum(event.event_type == AccountEventType.ACK.value for event in events) == 1
    assert sum(event.event_type == AccountEventType.ACK_OBSERVATION.value for event in events) == 1
    replayed = _kernel(tmp_path).state()
    assert replayed.orders[command.command_id].ack_accepted is True
    assert replayed.orders[command.command_id].ack_request_timing_observed is True
    assert replayed.orders[command.command_id].venue_order_id == "venue-late-http"
    assert (
        _kernel(tmp_path).record_ack(
            command_id=command.command_id,
            accepted=True,
            venue_order_id="venue-late-http",
            exchange_ts_ns=1_203_000_000,
            local_ack_ts_ns=1_216_000_000,
            metadata={
                "local_socket_send_ts_ns": 1_201_000_000,
                "source": "bybit_create_response",
            },
        )
        == ()
    )


def test_racing_ack_observers_reject_conflicting_durable_facts(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    result = kernel.submit_targets(
        batch_id="conflicting-ack",
        market_inputs=[_market()],
        targets=[_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=2.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-1",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_210_000_000,
    )

    with pytest.raises(account_kernel_module.AccountTransitionError, match="venue order id changed"):
        _kernel(tmp_path).record_ack(
            command_id=command.command_id,
            accepted=True,
            venue_order_id="venue-2",
            exchange_ts_ns=1_200_000_001,
            local_ack_ts_ns=1_210_000_001,
        )
    with pytest.raises(account_kernel_module.AccountTransitionError, match="acceptance changed"):
        _kernel(tmp_path).record_ack(
            command_id=command.command_id,
            accepted=False,
            venue_order_id="",
            exchange_ts_ns=1_200_000_002,
            local_ack_ts_ns=1_210_000_002,
            rejection_key="late-reject",
        )


def test_racing_fill_redelivery_ignores_local_provenance_but_not_venue_facts(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    result = kernel.submit_targets(
        batch_id="racing-fill",
        market_inputs=[_market()],
        targets=[_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=2.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-fill",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_210_000_000,
    )
    stale_observer = _kernel(tmp_path)
    kernel.record_fill(
        command_id=command.command_id,
        execution_id="execution-race",
        signed_qty=1.0,
        price=10.1,
        fee_usdt=0.001,
        exchange_ts_ns=1_220_000_000,
        local_receive_ts_ns=1_225_000_000,
        metadata={"source": "ws"},
    )

    assert (
        stale_observer.record_fill(
            command_id=command.command_id,
            execution_id="execution-race",
            signed_qty=1.0,
            price=10.1,
            fee_usdt=0.001,
            exchange_ts_ns=1_220_000_000,
            local_receive_ts_ns=1_230_000_000,
            metadata={"source": "rest_recovery"},
        )
        == ()
    )
    with pytest.raises(account_kernel_module.AccountTransitionError, match="changed price"):
        stale_observer.record_fill(
            command_id=command.command_id,
            execution_id="execution-race",
            signed_qty=1.0,
            price=10.2,
            fee_usdt=0.001,
            exchange_ts_ns=1_220_000_000,
            local_receive_ts_ns=1_230_000_001,
        )


def test_native_protection_fill_atomically_zeros_targets_and_records_pnl(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    opened = kernel.submit_targets(
        batch_id="open-short",
        market_inputs=[_market()],
        targets=[
            _target(
                decision="open-d1",
                key="continuous/strategy/trade-1/BUSDT",
                sleeve="continuous",
                qty=-2.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    command = opened.commands[0]
    driver = KernelExecutionDriver(kernel)
    driver.ingest(
        [
            {
                "observation_type": "ack",
                "command_id": command.command_id,
                "exchange_ts_ns": 1_200_000_000,
                "local_receive_ts_ns": 1_210_000_000,
                "accepted": True,
                "venue_order_id": "entry-order",
            },
            {
                "observation_type": "fill",
                "command_id": command.command_id,
                "exchange_ts_ns": 1_220_000_000,
                "local_receive_ts_ns": 1_225_000_000,
                "venue_order_id": "entry-order",
                "execution_id": "entry-exec",
                "signed_qty": -2.0,
                "price": 10.0,
                "fee_usdt": 0.01,
            },
        ]
    )
    kernel.record_protection(
        protection_key="native:BUSDT:one",
        symbol="BUSDT",
        status="active",
        stop_price=10.7,
        take_profit_price=None,
        exchange_ts_ns=1_230_000_000,
        local_receive_ts_ns=1_235_000_000,
        metadata={"native_exchange": True},
    )

    before = len(read_account_journal(tmp_path))
    adopted = kernel.adopt_external_protection_fill(
        protection_key="native:BUSDT:one",
        venue_order_id="bybit-stop-order",
        execution_id="stop-exec-1",
        symbol="BUSDT",
        signed_qty=2.0,
        price=11.0,
        fee_usdt=0.02,
        exchange_ts_ns=1_300_000_000,
        local_receive_ts_ns=1_305_000_000,
        metadata={"source": "test-native-stop"},
    )

    state = kernel.state()
    assert "continuous/strategy/trade-1/BUSDT" not in state.component_targets
    assert any(
        event.event_type == AccountEventType.TARGET.value
        and event.payload.get("target_key") == "continuous/strategy/trade-1/BUSDT"
        and float(event.payload.get("signed_qty") or 0.0) == 0.0
        for event in read_account_journal(tmp_path)
    )
    assert state.aggregate_targets["BUSDT"] == 0.0
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert len(state.closes) == 1
    pnl = next(iter(state.pnl.values()))
    assert pnl["gross_pnl_usdt"] == pytest.approx(-2.0)
    assert pnl["fee_usdt"] == pytest.approx(0.03)
    assert pnl["net_pnl_usdt"] == pytest.approx(-2.03)
    assert pnl["source"] == "fill_reconstructed_provisional_funding"
    assert [event.event_type for event in adopted] == [
        AccountEventType.MARKET_INPUT_REF.value,
        AccountEventType.DECISION.value,
        AccountEventType.TARGET.value,
        AccountEventType.RISK_DECISION.value,
        AccountEventType.ORDER_COMMAND.value,
        AccountEventType.ACK.value,
        AccountEventType.FILL.value,
        AccountEventType.PROTECTION.value,
        AccountEventType.CLOSE.value,
        AccountEventType.PNL.value,
    ]

    retry = kernel.adopt_external_protection_fill(
        protection_key="native:BUSDT:one",
        venue_order_id="bybit-stop-order",
        execution_id="stop-exec-1",
        symbol="BUSDT",
        signed_qty=2.0,
        price=11.0,
        fee_usdt=0.02,
        exchange_ts_ns=1_300_000_000,
        local_receive_ts_ns=1_305_000_000,
    )
    assert retry == ()
    assert len(read_account_journal(tmp_path)) == before + len(adopted)


def test_venue_delisting_fill_is_an_allowed_external_reduction_origin(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    opened = kernel.submit_targets(
        batch_id="open-before-delisting",
        market_inputs=[_market()],
        targets=[
            _target(
                decision="open-before-delisting",
                key="continuous/strategy/trade-delisting/BUSDT",
                sleeve="continuous",
                qty=-2.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    command = opened.commands[0]
    KernelExecutionDriver(kernel).ingest(
        [
            {
                "observation_type": "ack",
                "command_id": command.command_id,
                "exchange_ts_ns": 1_200_000_000,
                "local_receive_ts_ns": 1_210_000_000,
                "accepted": True,
                "venue_order_id": "entry-before-delisting",
            },
            {
                "observation_type": "fill",
                "command_id": command.command_id,
                "exchange_ts_ns": 1_220_000_000,
                "local_receive_ts_ns": 1_225_000_000,
                "venue_order_id": "entry-before-delisting",
                "execution_id": "entry-exec-before-delisting",
                "signed_qty": -2.0,
                "price": 10.0,
                "fee_usdt": 0.0,
            },
        ]
    )

    adopted = kernel.adopt_external_protection_fill(
        protection_key="venue-delisting:BUSDT:1234",
        venue_order_id="venue-delisting:BUSDT:1234",
        execution_id="venue-delisting-execution:BUSDT:1234",
        symbol="BUSDT",
        signed_qty=2.0,
        price=10.5,
        fee_usdt=0.0,
        exchange_ts_ns=1_300_000_000,
        local_receive_ts_ns=1_305_000_000,
        reason="venue_delisting_settlement",
        execution_origin="venue_delisting_settlement",
        metadata={"proxy_exactness": "structural"},
    )

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert state.aggregate_targets["BUSDT"] == 0.0
    fill = next(event for event in adopted if event.event_type == AccountEventType.FILL.value)
    assert fill.payload["metadata"]["external_execution_origin"] == ("venue_delisting_settlement")
    assert fill.payload["metadata"]["proxy_exactness"] == "structural"


def _kernel_holding_a_short(
    tmp_path: Path, *, qty: float = -2.0
) -> tuple[AccountExecutionKernel, str]:
    """Open one short position through the ordinary command path."""

    kernel = _kernel(tmp_path)
    opened = kernel.submit_targets(
        batch_id="open-before-external",
        market_inputs=[_market()],
        targets=[
            _target(
                decision="open-before-external",
                key="continuous/strategy/external/BUSDT",
                sleeve="continuous",
                qty=qty,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    command = opened.commands[0]
    KernelExecutionDriver(kernel).ingest(
        [
            {
                "observation_type": "ack",
                "command_id": command.command_id,
                "exchange_ts_ns": 1_200_000_000,
                "local_receive_ts_ns": 1_210_000_000,
                "accepted": True,
                "venue_order_id": "entry-before-external",
            },
            {
                "observation_type": "fill",
                "command_id": command.command_id,
                "exchange_ts_ns": 1_220_000_000,
                "local_receive_ts_ns": 1_225_000_000,
                "venue_order_id": "entry-before-external",
                "execution_id": "entry-exec-before-external",
                "signed_qty": qty,
                "price": 10.0,
                "fee_usdt": 0.0,
            },
        ]
    )
    return kernel, command.command_id


def test_an_external_reduction_larger_than_the_book_clamps_it_to_flat(tmp_path: Path) -> None:
    """The venue nets exposure this book never opened; book only our share.

    Refusing the whole row left the book short against a flat venue for hours
    and blocked every new entry on the account (ACEUSDT, 2026-08-07).
    """

    kernel, _command_id = _kernel_holding_a_short(tmp_path)

    adopted = kernel.adopt_external_protection_fill(
        protection_key="external-reduction:BUSDT:manual-close",
        venue_order_id="manual-close",
        execution_id="manual-close-exec-1",
        symbol="BUSDT",
        signed_qty=9.0,
        price=10.5,
        fee_usdt=9.0,
        exchange_ts_ns=1_300_000_000,
        local_receive_ts_ns=1_305_000_000,
        execution_origin="unattributed_external_reduction",
    )

    state = kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    fill = next(event for event in adopted if event.event_type == AccountEventType.FILL.value)
    assert fill.payload["signed_qty"] == 2.0
    # The fee follows the quantity it paid for, not the venue's whole row.
    assert fill.payload["fee_usdt"] == 2.0
    metadata = fill.payload["metadata"]
    assert metadata["venue_execution_signed_qty"] == 9.0
    assert metadata["foreign_signed_qty"] == 7.0
    # The synthetic order is terminal, so it never becomes a stalled wedge.
    adopted_order = next(
        order for order in state.orders.values() if order.venue_order_id == "manual-close"
    )
    assert adopted_order.status == "filled"


def test_one_venue_order_adopts_as_one_command_across_protection_keys(tmp_path: Path) -> None:
    """``active(symbol)`` changes between executions; the venue order does not.

    Keying the synthetic command on the protection key split a single manual
    close into two commands, both left partially filled forever.
    """

    kernel, _command_id = _kernel_holding_a_short(tmp_path, qty=-4.0)
    shared = dict(
        venue_order_id="manual-close",
        symbol="BUSDT",
        price=10.5,
        fee_usdt=0.0,
        exchange_ts_ns=1_300_000_000,
        local_receive_ts_ns=1_305_000_000,
        execution_origin="unattributed_external_reduction",
    )

    kernel.adopt_external_protection_fill(
        protection_key="native-disaster:BUSDT:abc:activation:0002",
        execution_id="manual-close-exec-1",
        signed_qty=1.0,
        **shared,
    )
    kernel.adopt_external_protection_fill(
        protection_key="external-reduction:BUSDT:manual-close",
        execution_id="manual-close-exec-2",
        signed_qty=3.0,
        **shared,
    )

    state = kernel.state()
    adopted = [order for order in state.orders.values() if order.venue_order_id == "manual-close"]
    assert len(adopted) == 1
    assert adopted[0].status == "filled"
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert not state.working_order_ids


def test_same_seed_and_input_tape_produce_identical_events_and_state_hashes(tmp_path: Path) -> None:
    roots = [tmp_path / "historical", tmp_path / "paper", tmp_path / "demo"]
    event_rows: list[list[dict[str, object]]] = []
    for root in roots:
        result = _kernel(root, account_id="parity-account").submit_targets(
            batch_id="parity-batch",
            market_inputs=[_market()],
            targets=[_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=-2.0)],
            risk_snapshot=_snapshot(),
            risk_policy=_policy(),
            instrument_rules=_rules(),
        )
        assert result.accepted
        event_rows.append([event.to_dict() for event in read_account_journal(root)])

    assert event_rows[0] == event_rows[1] == event_rows[2]
    assert [row["state_hash"] for row in event_rows[0]] == [row["state_hash"] for row in event_rows[2]]


def test_duplicate_batch_is_idempotent(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    kwargs = {
        "batch_id": "same-batch",
        "market_inputs": [_market()],
        "targets": [_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=-2.0)],
        "risk_snapshot": _snapshot(),
        "risk_policy": _policy(),
        "instrument_rules": _rules(),
    }
    first = kernel.submit_targets(**kwargs)
    second = kernel.submit_targets(**kwargs)
    assert first.accepted and second.accepted
    assert second.events == ()
    assert second.commands == first.commands
    assert len(read_account_journal(tmp_path)) == len(first.events)


def test_duplicate_batch_id_rejects_changed_request_content(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    common = {
        "batch_id": "same-batch",
        "market_inputs": [_market()],
        "risk_snapshot": _snapshot(),
        "risk_policy": _policy(),
        "instrument_rules": _rules(),
    }
    first = kernel.submit_targets(
        **common,
        targets=[
            _target(
                decision="d1",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=-2.0,
            )
        ],
    )
    before = read_account_journal(tmp_path)

    with pytest.raises(AccountJournalIntegrityError, match="request content changed"):
        kernel.submit_targets(
            **common,
            targets=[
                _target(
                    decision="d2",
                    key="continuous/main/BUSDT",
                    sleeve="continuous",
                    qty=-3.0,
                )
            ],
        )

    assert first.accepted
    assert read_account_journal(tmp_path) == before


def test_duplicate_batch_reuses_first_evaluation_when_snapshots_change(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    targets = [
        _target(
            decision="d1",
            key="continuous/main/BUSDT",
            sleeve="continuous",
            qty=-2.0,
        )
    ]
    first = kernel.submit_targets(
        batch_id="recovered-batch",
        market_inputs=[_market(price=10.0, key="first-book")],
        targets=targets,
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    before = read_account_journal(tmp_path)

    recovered = kernel.submit_targets(
        batch_id="recovered-batch",
        market_inputs=[_market(price=20.0, key="later-book")],
        targets=targets,
        risk_snapshot=AccountRiskSnapshot(
            equity_usdt=5_000.0,
            available_margin_usdt=4_000.0,
            snapshot_key="later-wallet",
            snapshot_ts_ns=999,
        ),
        risk_policy=AccountRiskPolicy(
            max_component_gross_notional_usdt=1.0,
            max_account_gross_notional_usdt=1.0,
            max_symbol_notional_usdt=1.0,
            max_initial_margin_usdt=1.0,
            max_leverage=1.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT",
                qty_step=1.0,
                min_qty=100.0,
                min_notional=100_000.0,
            )
        },
    )

    assert first.accepted and recovered.accepted
    assert recovered.events == ()
    assert recovered.commands == first.commands
    assert read_account_journal(tmp_path) == before


def test_projection_without_transaction_segments_requires_explicit_reset(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path / "source")
    kernel.submit_targets(
        batch_id="crash-boundaries",
        market_inputs=[_market()],
        targets=[
            _target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=-2.0),
            _target(decision="d2", key="hedge/main/BUSDT", sleeve="hedge", qty=1.0),
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    projection_root = tmp_path / "projection-only"
    path = account_journal_path(projection_root)
    path.parent.mkdir(parents=True)
    path.write_bytes(account_journal_path(tmp_path / "source").read_bytes())

    with pytest.raises(AccountJournalIntegrityError, match="reset the account root explicitly"):
        read_account_journal(projection_root)


def test_atomic_transaction_segments_remain_authoritative_if_jsonl_projection_is_torn(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    result = kernel.submit_targets(
        batch_id="atomic-batch",
        market_inputs=[_market()],
        targets=[_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=-2.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    transaction_paths = sorted(account_transactions_path(tmp_path).glob("*.json"))
    assert len(transaction_paths) == 1
    assert len(result.events) > 1  # the whole pre-execution batch is one atomic segment
    expected = [event.to_dict() for event in read_account_journal(tmp_path)]

    account_journal_path(tmp_path).write_bytes(b'{"torn":')
    assert [event.to_dict() for event in read_account_journal(tmp_path)] == expected

    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-atomic",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_201_000_000,
    )
    assert len(list(account_transactions_path(tmp_path).glob("*.json"))) == 2


def test_account_journal_head_reads_and_authenticates_only_latest_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    kernel.record_venue_snapshot(
        snapshot_key="head-1",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=2,
    )
    kernel.record_venue_snapshot(
        snapshot_key="head-2",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=3,
        local_receive_ts_ns=4,
    )
    expected = read_account_journal(tmp_path)[-1]
    observed_files: list[tuple[str, ...]] = []
    original_reader = account_kernel_module._read_transaction_event_bytes

    def observe_latest(files: object):
        rows = tuple(files)  # type: ignore[arg-type]
        observed_files.append(tuple(label for label, _data in rows))
        return original_reader(rows)

    monkeypatch.setattr(
        account_kernel_module,
        "_read_transaction_event_bytes",
        observe_latest,
    )

    assert read_account_journal_head(tmp_path) == expected
    assert len(observed_files) == 1
    assert len(observed_files[0]) == 1
    assert observed_files[0][0].endswith(sorted(account_transactions_path(tmp_path).glob("*.json"))[-1].name)


def test_account_journal_head_rejects_self_hashed_latest_event_tampering(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    kernel.record_venue_snapshot(
        snapshot_key="head-tamper",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=2,
    )
    transaction = next(account_transactions_path(tmp_path).glob("*.json"))
    payload = json.loads(transaction.read_bytes())
    payload["events"][-1]["state_hash"] = "f" * 64
    payload["transaction_hash"] = account_kernel_module._transaction_hash(payload)
    renamed = transaction.with_name(
        f"{int(payload['first_sequence']):020d}-{int(payload['last_sequence']):020d}-"
        f"{payload['transaction_hash'][:16]}.json"
    )
    transaction.rename(renamed)
    renamed.write_bytes(account_kernel_module.canonical_json(payload) + b"\n")

    with pytest.raises(AccountJournalIntegrityError, match="event hash mismatch"):
        read_account_journal_head(tmp_path)


def test_account_journal_reader_sees_only_committed_state_while_write_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    kernel.record_venue_snapshot(
        snapshot_key="committed",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=2,
    )
    committed_events = kernel.journal.events()
    committed_hash = kernel.state().state_hash()
    write_entered = threading.Event()
    release_write = threading.Event()
    reader_done = threading.Event()
    writer_errors: list[BaseException] = []
    reader_result: dict[str, object] = {}
    real_write_transaction = account_kernel_module._write_transaction

    def blocking_write_transaction(
        root: str | Path,
        events: list[account_kernel_module.AccountEvent],
    ) -> Path:
        if any(event.correlation_id == "prospective" for event in events):
            write_entered.set()
            if not release_write.wait(timeout=5.0):
                raise TimeoutError("test did not release blocked account write")
        return real_write_transaction(root, events)

    monkeypatch.setattr(
        account_kernel_module,
        "_write_transaction",
        blocking_write_transaction,
    )

    def write_prospective_snapshot() -> None:
        try:
            kernel.record_venue_snapshot(
                snapshot_key="prospective",
                venue_positions={"BUSDT": 1.0},
                reconstructed_positions={},
                mismatches=["BUSDT"],
                exchange_ts_ns=3,
                local_receive_ts_ns=4,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)

    def read_during_write() -> None:
        reader_result["events"] = kernel.journal.events()
        reader_result["state"] = kernel.state()
        reader_done.set()

    writer = threading.Thread(target=write_prospective_snapshot)
    writer.start()
    assert write_entered.wait(timeout=2.0)
    reader = threading.Thread(target=read_during_write)
    reader.start()
    assert reader_done.wait(timeout=2.0)

    observed_events = reader_result["events"]
    observed_state = reader_result["state"]
    assert isinstance(observed_events, list)
    assert isinstance(observed_state, account_kernel_module.AccountState)
    assert observed_events == committed_events
    assert observed_state.state_hash() == committed_hash
    assert "prospective" not in observed_state.venue_snapshots
    assert read_account_journal(tmp_path) == committed_events

    release_write.set()
    writer.join(timeout=5.0)
    reader.join(timeout=5.0)
    assert not writer.is_alive()
    assert not reader.is_alive()
    assert writer_errors == []
    assert "prospective" in kernel.state().venue_snapshots


def test_account_journal_reader_does_not_replay_history_between_segment_commit_and_cache_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hot reader must not turn the atomic-replace/cache-publish gap into
    an O(history) critical section that stalls every journal writer."""

    kernel = _kernel(tmp_path)
    kernel.record_venue_snapshot(
        snapshot_key="committed-before-publication-race",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=2,
    )
    committed_events = kernel.journal.events()
    committed_hash = kernel.state().state_hash()
    segment_committed = threading.Event()
    release_cache_publication = threading.Event()
    reader_done = threading.Event()
    writer_errors: list[BaseException] = []
    reader_errors: list[BaseException] = []
    reader_result: dict[str, object] = {}
    real_write_transaction = account_kernel_module._write_transaction

    def committed_then_blocked_write(
        root: str | Path,
        events: list[account_kernel_module.AccountEvent],
    ) -> Path:
        path = real_write_transaction(root, events)
        if any(event.correlation_id == "prospective-after-segment" for event in events):
            segment_committed.set()
            if not release_cache_publication.wait(timeout=5.0):
                raise TimeoutError("test did not release account cache publication")
        return path

    def forbid_history_replay(_root: str | Path) -> list[AccountEvent] | None:
        raise AssertionError("reader replayed immutable history during local cache publication")

    monkeypatch.setattr(
        account_kernel_module,
        "_write_transaction",
        committed_then_blocked_write,
    )
    monkeypatch.setattr(
        account_kernel_module,
        "_read_transaction_events",
        forbid_history_replay,
    )

    def write_prospective_snapshot() -> None:
        try:
            kernel.record_venue_snapshot(
                snapshot_key="prospective-after-segment",
                venue_positions={"BUSDT": 1.0},
                reconstructed_positions={},
                mismatches=["BUSDT"],
                exchange_ts_ns=3,
                local_receive_ts_ns=4,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)

    def read_after_segment_commit() -> None:
        try:
            reader_result["events"] = kernel.journal.events()
            reader_result["state"] = kernel.state()
        except BaseException as exc:  # pragma: no cover - asserted below
            reader_errors.append(exc)
        finally:
            reader_done.set()

    writer = threading.Thread(target=write_prospective_snapshot)
    writer.start()
    assert segment_committed.wait(timeout=2.0)
    reader = threading.Thread(target=read_after_segment_commit)
    reader.start()
    assert reader_done.wait(timeout=2.0)

    assert reader_errors == []
    observed_events = reader_result["events"]
    observed_state = reader_result["state"]
    assert isinstance(observed_events, list)
    assert isinstance(observed_state, account_kernel_module.AccountState)
    assert observed_events == committed_events
    assert observed_state.state_hash() == committed_hash
    assert "prospective-after-segment" not in observed_state.venue_snapshots

    release_cache_publication.set()
    writer.join(timeout=5.0)
    reader.join(timeout=5.0)
    assert not writer.is_alive()
    assert not reader.is_alive()
    assert writer_errors == []
    assert "prospective-after-segment" in kernel.state().venue_snapshots


def test_cached_transaction_append_does_not_rescan_immutable_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    kernel.record_venue_snapshot(
        snapshot_key="first-cached-append",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=2,
    )
    transaction_directory = account_transactions_path(tmp_path)
    original_glob = Path.glob

    def forbid_transaction_reread(*_args: object, **_kwargs: object):
        raise AssertionError("cached append reparsed immutable transaction history")

    def forbid_transaction_glob(path: Path, pattern: str):
        if path == transaction_directory and pattern == "*.json":
            raise AssertionError("cached append rescanned immutable transaction paths")
        return original_glob(path, pattern)

    monkeypatch.setattr(
        account_kernel_module,
        "_read_transaction_events",
        forbid_transaction_reread,
    )
    monkeypatch.setattr(Path, "glob", forbid_transaction_glob)

    appended = kernel.record_venue_snapshot(
        snapshot_key="second-cached-append",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=3,
        local_receive_ts_ns=4,
    )

    assert len(appended) == 1
    assert "second-cached-append" in kernel._state_ref().venue_snapshots


def test_trusted_cached_append_does_not_deepcopy_historical_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    kernel.record_venue_snapshot(
        snapshot_key="first-shared-history-append",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=2,
        metadata={"large_immutable_history": [str(index) for index in range(1_000)]},
    )

    def forbid_deepcopy(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("trusted transaction deep-copied immutable history")

    monkeypatch.setattr(account_kernel_module.copy, "deepcopy", forbid_deepcopy)

    appended = kernel.record_venue_snapshot(
        snapshot_key="second-shared-history-append",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=3,
        local_receive_ts_ns=4,
    )

    assert len(appended) == 1
    assert list(kernel._state_ref().venue_snapshots) == ["second-shared-history-append"]


def test_snapshot_ref_resolves_one_event_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    kernel.record_venue_snapshot(
        snapshot_key="coherent-snapshot",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=2,
    )
    original_events_ref = kernel.journal._events_ref
    calls = 0

    def counted_events_ref() -> list[AccountEvent]:
        nonlocal calls
        calls += 1
        return original_events_ref()

    monkeypatch.setattr(kernel.journal, "_events_ref", counted_events_ref)

    events, state = kernel._snapshot_ref()

    assert calls == 1
    assert len(events) == state.events_applied
    assert events[-1].state_hash == state.rolling_state_hash


def test_materialized_state_keeps_only_latest_venue_snapshot(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    for sequence in (1, 2):
        kernel.record_venue_snapshot(
            snapshot_key=f"snapshot-{sequence}",
            venue_positions={},
            reconstructed_positions={},
            mismatches=[],
            exchange_ts_ns=sequence,
            local_receive_ts_ns=sequence,
        )

    snapshot_events = [
        event for event in kernel.journal.events() if event.event_type == AccountEventType.VENUE_SNAPSHOT.value
    ]
    assert [event.payload["snapshot_key"] for event in snapshot_events] == [
        "snapshot-1",
        "snapshot-2",
    ]
    assert list(kernel.state().venue_snapshots) == ["snapshot-2"]


def test_account_journal_failed_write_does_not_publish_prospective_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    kernel.record_venue_snapshot(
        snapshot_key="committed",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=2,
    )
    committed_events = kernel.journal.events()
    committed_hash = kernel.state().state_hash()

    def fail_write_transaction(
        root: str | Path,
        events: list[account_kernel_module.AccountEvent],
    ) -> Path:
        del root, events
        raise OSError("injected transaction write failure")

    monkeypatch.setattr(
        account_kernel_module,
        "_write_transaction",
        fail_write_transaction,
    )

    with pytest.raises(OSError, match="injected transaction write failure"):
        kernel.record_venue_snapshot(
            snapshot_key="prospective",
            venue_positions={"BUSDT": 1.0},
            reconstructed_positions={},
            mismatches=["BUSDT"],
            exchange_ts_ns=3,
            local_receive_ts_ns=4,
        )

    observed_state = kernel.state()
    assert kernel.journal.events() == committed_events
    assert read_account_journal(tmp_path) == committed_events
    assert observed_state.state_hash() == committed_hash
    assert "prospective" not in observed_state.venue_snapshots
    assert not kernel.journal._local_transaction_publish_in_progress


def test_account_journal_extends_committed_cache_without_history_sized_recopy(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    kernel.record_venue_snapshot(
        snapshot_key="snapshot-1",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=1,
    )
    cached_events = kernel.journal._cached_events
    cached_ids = kernel.journal._cached_events_by_id
    assert cached_events is not None
    assert cached_ids is not None

    kernel.record_venue_snapshot(
        snapshot_key="snapshot-2",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=2,
        local_receive_ts_ns=2,
    )

    assert kernel.journal._cached_events is cached_events
    assert kernel.journal._cached_events_by_id is cached_ids
    assert [
        event.payload["snapshot_key"]
        for event in read_account_journal(tmp_path)
        if event.event_type == AccountEventType.VENUE_SNAPSHOT.value
    ] == ["snapshot-1", "snapshot-2"]


def test_single_process_inplace_research_avoids_state_deepcopy_and_rejects_untrusted_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    kernel = AccountExecutionKernel(
        tmp_path / "inplace",
        account_id="acct",
        unsafe_single_process_inplace_research=True,
    )
    kernel.record_venue_snapshot(
        snapshot_key="snapshot-1",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=1,
    )
    committed_state = kernel._state_ref()

    def forbidden_deepcopy(_value: object) -> object:
        raise AssertionError("research transaction copied the accumulated account state")

    monkeypatch.setattr(account_kernel_module.copy, "deepcopy", forbidden_deepcopy)
    kernel.record_venue_snapshot(
        snapshot_key="snapshot-2",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=2,
        local_receive_ts_ns=2,
    )

    assert kernel._state_ref() is committed_state
    with pytest.raises(AccountKernelError, match="trusted read-only builder"):
        kernel.journal.transact(lambda _state: [])


def test_single_process_inplace_research_preserves_event_hashes(tmp_path: Path) -> None:
    baseline = AccountExecutionKernel(
        tmp_path / "baseline",
        account_id="acct",
        clock=VirtualClock(current_wall_ns=1, current_monotonic_ns=0),
    )
    inplace = AccountExecutionKernel(
        tmp_path / "inplace",
        account_id="acct",
        clock=VirtualClock(current_wall_ns=1, current_monotonic_ns=0),
        unsafe_single_process_inplace_research=True,
    )
    for kernel in (baseline, inplace):
        for index in (1, 2):
            kernel.record_venue_snapshot(
                snapshot_key=f"snapshot-{index}",
                venue_positions={},
                reconstructed_positions={},
                mismatches=[],
                exchange_ts_ns=index,
                local_receive_ts_ns=index,
            )

    assert [event.to_dict() for event in inplace.journal.events()] == [
        event.to_dict() for event in baseline.journal.events()
    ]
    assert inplace._state_ref().state_hash() == baseline._state_ref().state_hash()


def test_account_risk_revalues_existing_components_at_current_market_input(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    tight = AccountRiskPolicy(
        max_component_gross_notional_usdt=30.0,
        max_account_gross_notional_usdt=30.0,
        max_symbol_notional_usdt=30.0,
        max_initial_margin_usdt=10.0,
        max_leverage=10.0,
    )
    opened = kernel.submit_targets(
        batch_id="price-10",
        market_inputs=[_market(price=10.0)],
        targets=[_target(decision="d1", key="long/main/BUSDT", sleeve="long", qty=2.0, price=10.0)],
        risk_snapshot=_snapshot(),
        risk_policy=tight,
        instrument_rules=_rules(),
    )
    assert opened.accepted

    # The old component still says reference_price=10, but risk must value its
    # 2 units at the new $20 market input and reject $40 > $30.
    revalued = kernel.submit_targets(
        batch_id="price-20",
        market_inputs=[_market(price=20.0, key="book-2")],
        targets=[_target(decision="d2", key="hedge/main/BUSDT", sleeve="hedge", qty=0.0, price=20.0)],
        risk_snapshot=_snapshot(),
        risk_policy=tight,
        instrument_rules=_rules(),
    )
    assert not revalued.accepted
    assert "account-risk:price-20:component_gross_limit" in revalued.rejection_keys


def test_historical_paper_and_demo_adapters_share_full_ack_fill_state_hashes(tmp_path: Path) -> None:
    roots = [tmp_path / "historical", tmp_path / "paper", tmp_path / "demo"]
    rows: list[list[dict[str, object]]] = []
    for root, adapter_name in zip(roots, ("historical", "paper", "demo"), strict=True):
        kernel = _kernel(root, account_id="full-parity-account")
        market = _book().market_ref(input_key="l2-100")
        result = kernel.submit_targets(
            batch_id="full-parity",
            market_inputs=[market],
            targets=[_target(decision="d1", key="continuous/main/BUSDT", sleeve="continuous", qty=2.0)],
            risk_snapshot=_snapshot(),
            risk_policy=_policy(),
            instrument_rules=_rules(),
        )
        KernelExecutionDriver(kernel).execute_batch(
            result,
            market_inputs={"BUSDT": market},
            adapter=_twin(name=adapter_name),
        )
        rows.append([event.to_dict() for event in read_account_journal(root)])

    assert rows[0] == rows[1] == rows[2]
    assert [row["event_type"] for row in rows[0]][-3:] == [
        AccountEventType.ACK.value,
        AccountEventType.FILL.value,
        AccountEventType.ORDER_STATUS.value,
    ]


def test_complete_synchronous_commands_share_durable_execution_transactions(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    rules = _rules(max_order_qty=1.0)
    market = _book().market_ref(input_key="chunked-book")
    result = kernel.submit_targets(
        batch_id="chunked-open",
        market_inputs=[market],
        targets=[
            _target(
                decision="chunked-d",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=2.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
    )
    assert len(result.commands) == 2

    KernelExecutionDriver(kernel).execute_batch(
        result,
        market_inputs={"BUSDT": market},
        adapter=_twin(name="historical", rules=rules),
    )

    # One target transaction, one atomic ACK/fill transaction, and one terminal
    # status transaction. This avoids one fsync sequence per observation while
    # retaining every immutable event and state hash.
    assert len(list(account_transactions_path(tmp_path).glob("*.json"))) == 3
    assert kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    execution_types = [
        event.event_type
        for event in read_account_journal(tmp_path)
        if event.event_type
        in {
            AccountEventType.ACK.value,
            AccountEventType.FILL.value,
            AccountEventType.ORDER_STATUS.value,
        }
    ]
    assert execution_types == [
        AccountEventType.ACK.value,
        AccountEventType.FILL.value,
        AccountEventType.ACK.value,
        AccountEventType.FILL.value,
        AccountEventType.ORDER_STATUS.value,
        AccountEventType.ORDER_STATUS.value,
    ]
    receipt = verify_account_journal(tmp_path)
    assert receipt["events"] == len(read_account_journal(tmp_path))
    assert receipt["fills"] == 2
    assert receipt["transactions"] == 3
    assert receipt["final_state_hash"] == kernel.state().state_hash()


def test_execution_twin_walks_book_and_terminal_partial_fill_has_no_phantom_working_qty(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    market = _book(ask_qty=0.6).market_ref(input_key="thin-book")
    result = kernel.submit_targets(
        batch_id="partial-depth",
        market_inputs=[market],
        targets=[_target(decision="d1", key="long/main/BUSDT", sleeve="long", qty=2.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    KernelExecutionDriver(kernel).execute_batch(
        result,
        market_inputs={"BUSDT": market},
        adapter=_twin(name="historical", book=_book(ask_qty=0.6)),
    )
    state = kernel.state()
    command = result.commands[0]
    assert state.orders[command.command_id].status == "partially_filled_cancelled"
    assert state.positions["BUSDT"].signed_qty == pytest.approx(1.2)
    assert state.positions["BUSDT"].average_price == pytest.approx(10.15)
    assert state.working_signed_qty("BUSDT") == 0.0
    fills = [event for event in read_account_journal(tmp_path) if event.event_type == AccountEventType.FILL.value]
    assert fills[-1].payload["metadata"]["unfilled_cancelled_qty"] == pytest.approx(0.8)
    assert fills[-1].payload["metadata"]["immutable_replay_book"] is True


def test_execution_twin_rejects_sequence_gap_before_fill(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    book = _book(gap=True)
    market = _market(key="gapped-book")
    result = kernel.submit_targets(
        batch_id="gap-reject",
        market_inputs=[market],
        targets=[_target(decision="d1", key="long/main/BUSDT", sleeve="long", qty=2.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    KernelExecutionDriver(kernel).execute_batch(
        result,
        market_inputs={"BUSDT": market},
        adapter=_twin(name="historical", book=book),
    )
    state = kernel.state()
    order = state.orders[result.commands[0].command_id]
    assert order.status == "rejected"
    assert order.rejection_key.endswith(":book_sequence_gap")
    assert state.positions == {}


def test_protection_close_and_confirmed_pnl_follow_fills(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    market = _book().market_ref(input_key="open-book")
    opened = kernel.submit_targets(
        batch_id="open",
        market_inputs=[market],
        targets=[_target(decision="open-d", key="long/main/BUSDT", sleeve="long", qty=2.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    driver = KernelExecutionDriver(kernel)
    driver.execute_batch(opened, market_inputs={"BUSDT": market}, adapter=_twin(name="paper"))
    kernel.record_protection(
        protection_key="BUSDT-protection-v1",
        symbol="BUSDT",
        status="active",
        stop_price=9.0,
        take_profit_price=12.0,
        exchange_ts_ns=1_030_000_000,
        local_receive_ts_ns=1_035_000_000,
        command_id=opened.commands[0].command_id,
    )

    close_market = _book().market_ref(input_key="close-book")
    closing = kernel.submit_targets(
        batch_id="close",
        market_inputs=[close_market],
        targets=[_target(decision="close-d", key="long/main/BUSDT", sleeve="long", qty=0.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    assert closing.commands[0].reduce_only
    driver.execute_batch(closing, market_inputs={"BUSDT": close_market}, adapter=_twin(name="paper"))
    assert kernel.state().positions["BUSDT"].signed_qty == 0.0
    kernel.record_close(
        close_key="BUSDT-close-1",
        symbol="BUSDT",
        reason="take_profit",
        venue_flat=True,
        exchange_ts_ns=1_100_000_000,
        local_receive_ts_ns=1_105_000_000,
        command_id=closing.commands[0].command_id,
    )
    kernel.record_pnl(
        pnl_key="BUSDT-pnl-1",
        close_key="BUSDT-close-1",
        symbol="BUSDT",
        gross_pnl_usdt=-0.4,
        fee_usdt=0.022,
        funding_usdt=0.0,
        net_pnl_usdt=-0.422,
        exchange_ts_ns=1_110_000_000,
        local_receive_ts_ns=1_115_000_000,
        source="venue_closed_pnl",
    )
    assert [event.event_type for event in read_account_journal(tmp_path)][-2:] == [
        AccountEventType.CLOSE.value,
        AccountEventType.PNL.value,
    ]


def test_same_symbol_component_reductions_checkpoint_without_false_later_credit(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    driver = KernelExecutionDriver(kernel)

    def submit_and_fill(
        *,
        batch_id: str,
        component_id: str,
        target_qty: float,
        fill_price: float,
        reason: str,
    ) -> None:
        market = _market(price=fill_price, key=f"book-{batch_id}")
        result = kernel.submit_targets(
            batch_id=batch_id,
            market_inputs=[market],
            targets=[
                DesiredTarget(
                    decision_key=f"decision-{batch_id}",
                    target_key=f"continuous/strategy/{component_id}/BUSDT",
                    sleeve="continuous",
                    strategy_id="strategy",
                    component_id=component_id,
                    symbol="BUSDT",
                    signed_qty=target_qty,
                    reference_price=fill_price,
                    leverage=10.0,
                    reason=reason,
                )
            ],
            risk_snapshot=_snapshot(),
            risk_policy=_policy(),
            instrument_rules=_rules(),
        )
        command = result.commands[0]
        driver.ingest(
            (
                ExecutionObservation(
                    observation_type="ack",
                    command_id=command.command_id,
                    exchange_ts_ns=1,
                    local_receive_ts_ns=2,
                    accepted=True,
                    venue_order_id=f"venue-{batch_id}",
                ),
                ExecutionObservation(
                    observation_type="fill",
                    command_id=command.command_id,
                    exchange_ts_ns=3,
                    local_receive_ts_ns=4,
                    venue_order_id=f"venue-{batch_id}",
                    execution_id=f"execution-{batch_id}",
                    signed_qty=command.signed_qty,
                    price=fill_price,
                    fee_usdt=0.01,
                    metadata={
                        "fee_observed": True,
                        "fee_status": "observed_execution_fee",
                        "fee_source": "test_execution_fee",
                        "source": "test_venue_execution",
                    },
                ),
            )
        )

    submit_and_fill(
        batch_id="open-a",
        component_id="component-a",
        target_qty=1.0,
        fill_price=10.0,
        reason="entry",
    )
    submit_and_fill(
        batch_id="open-b",
        component_id="component-b",
        target_qty=1.0,
        fill_price=20.0,
        reason="entry",
    )
    submit_and_fill(
        batch_id="take-profit-a",
        component_id="component-a",
        target_qty=0.0,
        fill_price=12.0,
        reason="take_profit",
    )

    state_after_a = kernel.state()
    assert state_after_a.positions["BUSDT"].signed_qty == pytest.approx(1.0)
    assert len(state_after_a.pnl) == 1
    first = next(iter(state_after_a.pnl.values()))
    first_close = state_after_a.closes[str(first["close_key"])]
    assert first_close["venue_flat"] is False
    assert first["gross_pnl_usdt"] == pytest.approx(-3.0)
    assert first["fee_usdt"] == pytest.approx(0.03)
    assert first["net_pnl_usdt"] == pytest.approx(-3.03)
    assert first["metadata"]["component_ids"] == ["component-a"]
    assert first["metadata"]["component_attribution_status"] == "pending_account_netting"
    assert first["metadata"]["fee_status"] == "observed_execution_fee"
    assert first["metadata"]["funding_status"] == "pending_venue_reconciliation"
    assert first["metadata"]["venue_closed_pnl_status"] == "pending_venue_reconciliation"

    submit_and_fill(
        batch_id="take-profit-b",
        component_id="component-b",
        target_qty=0.0,
        fill_price=18.0,
        reason="time_stop",
    )

    final = kernel.state()
    assert final.positions["BUSDT"].signed_qty == 0.0
    pnl_by_batch = {str(row["metadata"]["batch_id"]): row for row in final.pnl.values()}
    second = pnl_by_batch["take-profit-b"]
    assert second["gross_pnl_usdt"] == pytest.approx(3.0)
    assert second["fee_usdt"] == pytest.approx(0.01)
    assert second["net_pnl_usdt"] == pytest.approx(2.99)
    assert second["metadata"]["component_ids"] == ["component-b"]
    second_close = final.closes[str(second["close_key"])]
    assert second_close["venue_flat"] is False
    assert second_close["metadata"]["reconstructed_flat"] is True
    assert second_close["metadata"]["venue_position_status"] == "pending_reconciliation"
    # Component A's earlier realized loss was checkpointed under its own
    # reduction batch; it is not silently rolled into component B's later row.
    assert sum(float(row["net_pnl_usdt"]) for row in final.pnl.values()) == pytest.approx(-0.04)


def test_reduce_batch_finalization_is_atomic_under_concurrent_redelivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    opened = kernel.submit_targets(
        batch_id="open-before-concurrent-close",
        market_inputs=[_market()],
        targets=[
            _target(
                decision="open-before-concurrent-close",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    open_command = opened.commands[0]
    kernel.record_ack(
        command_id=open_command.command_id,
        accepted=True,
        venue_order_id="venue-open-before-concurrent-close",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_201_000_000,
    )
    kernel.record_fill(
        command_id=open_command.command_id,
        execution_id="fill-open-before-concurrent-close",
        signed_qty=1.0,
        price=10.0,
        fee_usdt=0.01,
        exchange_ts_ns=1_300_000_000,
        local_receive_ts_ns=1_301_000_000,
    )
    closed = kernel.submit_targets(
        batch_id="concurrent-close",
        market_inputs=[_market(price=11.0, key="book-concurrent-close")],
        targets=[
            _target(
                decision="concurrent-close",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=0.0,
                price=11.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    close_command = closed.commands[0]
    assert close_command.reduce_only
    kernel.record_ack(
        command_id=close_command.command_id,
        accepted=True,
        venue_order_id="venue-concurrent-close",
        exchange_ts_ns=1_400_000_000,
        local_ack_ts_ns=1_401_000_000,
    )
    kernel.record_fill(
        command_id=close_command.command_id,
        execution_id="fill-concurrent-close",
        signed_qty=-1.0,
        price=11.0,
        fee_usdt=0.01,
        exchange_ts_ns=1_500_000_000,
        local_receive_ts_ns=1_501_000_000,
    )

    transaction_count = len(list(account_transactions_path(tmp_path).glob("*.json")))
    transaction_barrier = threading.Barrier(2)
    original_transact = kernel.journal.transact

    def synchronized_transact(
        builder: Callable[
            [account_kernel_module.AccountState],
            Iterable[account_kernel_module.AccountEventSpec],
        ],
        *,
        trusted_readonly_builder: bool = False,
    ) -> list[account_kernel_module.AccountEvent]:
        transaction_barrier.wait(timeout=5.0)
        return original_transact(
            builder,
            trusted_readonly_builder=trusted_readonly_builder,
        )

    monkeypatch.setattr(kernel.journal, "transact", synchronized_transact)
    results: list[tuple[account_kernel_module.AccountEvent, ...]] = []
    errors: list[BaseException] = []

    def finalize(local_receive_ts_ns: int) -> None:
        try:
            results.append(
                kernel.finalize_flat_position(
                    symbol="BUSDT",
                    command_id=close_command.command_id,
                    exchange_ts_ns=local_receive_ts_ns - 1,
                    local_receive_ts_ns=local_receive_ts_ns,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=finalize, args=(1_600_000_000 + offset,)) for offset in (0, 1)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10.0)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert sorted(len(result) for result in results) == [0, 2]
    assert len(list(account_transactions_path(tmp_path).glob("*.json"))) == transaction_count + 1
    final = kernel.state()
    assert len(final.closes) == 1
    assert len(final.pnl) == 1


def test_bybit_adapter_refuses_an_unrealmed_client_and_never_synthesizes_a_fill() -> None:
    class FakeClient:
        # No ``realm``, so it reads as demo, and ``demo=False`` contradicts it.
        # The adapter accepts either realm now, but never a client whose
        # declared realm and transport disagree.
        demo = False

    with pytest.raises(ValueError, match="contradicts its demo realm"):
        BybitDemoExecutionAdapter(FakeClient())

    class DemoClient:
        demo = True

        def set_leverage(self, **params: object) -> dict[str, object]:
            assert params == {
                "symbol": "BUSDT",
                "buy_leverage": 1.0,
                "sell_leverage": 1.0,
            }
            return {}

        def place_order(self, **params: object) -> dict[str, str]:
            assert params["orderLinkId"]
            assert params["stopLoss"] == "8"
            assert params["slTriggerBy"] == "MarkPrice"
            assert params["tpslMode"] == "Full"
            assert params["slOrderType"] == "Market"
            return {"orderId": "venue-demo-1", "_response_time_ms": "2000"}

    adapter = BybitDemoExecutionAdapter(
        DemoClient(),
        clock=VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=50),
    )
    observations = tuple(
        adapter.submit(
            OrderCommand(
                command_id="11111111-1111-5111-8111-111111111111",
                batch_id="b1",
                symbol="BUSDT",
                side="Buy",
                qty=0.1,
                signed_qty=0.1,
                reduce_only=False,
                reference_price=10.0,
                target_signed_qty=0.1,
                chunk_index=0,
                chunk_count=1,
                entry_stop_price=8.0,
                entry_stop_fraction=0.2,
                entry_stop_source="test_entry_attached_stop",
                entry_stop_trigger_by="MarkPrice",
            ),
            _market(),
        )
    )
    assert len(observations) == 1
    assert observations[0].observation_type == "ack"
    assert observations[0].accepted
    assert observations[0].venue_order_id == "venue-demo-1"
    assert observations[0].exchange_ts_ns == 2_000_000_000
    assert observations[0].metadata["exchange_ack_ts_status"] == "observed"


def test_bybit_demo_adapter_verifies_the_attached_stop_after_the_create() -> None:
    """B5: an exposure-increasing create is followed by a venue read-back."""

    class DemoClient:
        demo = True

        def set_leverage(self, **_params: object) -> dict[str, object]:
            return {}

        def place_order(self, **_params: object) -> dict[str, str]:
            return {"orderId": "venue-demo-1", "_response_time_ms": "2000"}

    seen: list[dict[str, object]] = []

    def verifier(
        *,
        symbol: str,
        expected_stop_price: float,
        command_id: str,
        acknowledged_ts_ns: int = 0,
    ) -> str:
        seen.append(
            {
                "symbol": symbol,
                "expected_stop_price": expected_stop_price,
                "command_id": command_id,
            }
        )
        return "armed"

    entry = OrderCommand(
        command_id="33333333-3333-5333-8333-333333333333",
        batch_id="verify-batch",
        symbol="BUSDT",
        side="Buy",
        qty=0.1,
        signed_qty=0.1,
        reduce_only=False,
        reference_price=10.0,
        target_signed_qty=0.1,
        chunk_index=0,
        chunk_count=1,
        entry_stop_price=8.0,
        entry_stop_fraction=0.2,
        entry_stop_source="test_entry_attached_stop",
        entry_stop_trigger_by="MarkPrice",
    )
    adapter = BybitDemoExecutionAdapter(
        DemoClient(),
        clock=VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=50),
        entry_stop_verifier=verifier,
    )
    observation = tuple(adapter.submit(entry, _market()))[0]

    assert observation.metadata["entry_attached_stop_verification"] == "armed"
    assert seen == [
        {
            "symbol": "BUSDT",
            "expected_stop_price": 8.0,
            "command_id": "33333333-3333-5333-8333-333333333333",
        }
    ]

    # A reduce-only exit carries no attached stop, so there is nothing to prove.
    exit_command = OrderCommand(
        command_id="44444444-4444-5444-8444-444444444444",
        batch_id="verify-batch",
        symbol="BUSDT",
        side="Sell",
        qty=0.1,
        signed_qty=-0.1,
        reduce_only=True,
        reference_price=10.0,
        target_signed_qty=0.0,
        chunk_index=0,
        chunk_count=1,
    )
    exit_observation = tuple(adapter.submit(exit_command, _market()))[0]
    assert exit_observation.metadata["entry_attached_stop_verification"] == "not_applicable"
    assert len(seen) == 1


def test_bybit_demo_adapter_keeps_the_ack_when_the_verifier_itself_faults() -> None:
    """Losing an ACK would orphan a live position; the verifier owns the fail-closed."""

    class DemoClient:
        demo = True

        def set_leverage(self, **_params: object) -> dict[str, object]:
            return {}

        def place_order(self, **_params: object) -> dict[str, str]:
            return {"orderId": "venue-demo-2", "_response_time_ms": "2000"}

    def exploding_verifier(**_kwargs: object) -> str:
        raise TimeoutError("venue read timed out")

    observation = tuple(
        BybitDemoExecutionAdapter(
            DemoClient(),
            clock=VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=50),
            entry_stop_verifier=exploding_verifier,
        ).submit(
            OrderCommand(
                command_id="55555555-5555-5555-8555-555555555555",
                batch_id="verify-batch",
                symbol="BUSDT",
                side="Buy",
                qty=0.1,
                signed_qty=0.1,
                reduce_only=False,
                reference_price=10.0,
                target_signed_qty=0.1,
                chunk_index=0,
                chunk_count=1,
                entry_stop_price=8.0,
                entry_stop_fraction=0.2,
                entry_stop_source="test_entry_attached_stop",
                entry_stop_trigger_by="MarkPrice",
            ),
            _market(),
        )
    )[0]

    assert observation.accepted
    assert observation.venue_order_id == "venue-demo-2"
    assert observation.metadata["entry_attached_stop_verification"] == "verifier_failed:TimeoutError"


def test_bybit_demo_adapter_times_create_after_leverage_negotiation() -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000)

    class DemoClient:
        demo = True

        def set_leverage(self, **_params: object) -> dict[str, object]:
            clock.advance_ns(50_000_000)
            return {}

        def place_order(self, **_params: object) -> dict[str, str]:
            clock.advance_ns(5_000_000)
            return {
                "orderId": "venue-timed-1",
                "_response_time_ms": "2052",
            }

    command = OrderCommand(
        command_id="22222222-2222-5222-8222-222222222222",
        batch_id="timing-batch",
        symbol="BUSDT",
        side="Buy",
        qty=0.1,
        signed_qty=0.1,
        reduce_only=False,
        reference_price=10.0,
        target_signed_qty=0.1,
        chunk_index=0,
        chunk_count=1,
        entry_stop_price=8.0,
        entry_stop_fraction=0.2,
        entry_stop_source="test_entry_attached_stop",
        entry_stop_trigger_by="MarkPrice",
    )
    observation = tuple(
        BybitDemoExecutionAdapter(DemoClient(), clock=clock).submit(
            command,
            _market(),
        )
    )[0]

    assert observation.metadata["local_socket_send_ts_ns"] == 2_050_000_000
    assert observation.local_receive_ts_ns == 2_055_000_000
    assert observation.exchange_ts_ns == 2_052_000_000


def test_bybit_demo_adapter_refuses_naked_entry_before_any_mutation() -> None:
    class RecordingClient:
        demo = True

        def __init__(self) -> None:
            self.calls: list[str] = []

        def set_leverage(self, **_params: object) -> dict[str, object]:
            self.calls.append("set_leverage")
            return {}

        def place_order(self, **_params: object) -> dict[str, str]:
            self.calls.append("place_order")
            return {"orderId": "must-not-exist"}

    client = RecordingClient()
    with pytest.raises(
        RuntimeError,
        match="lacks durable entry-attached protection",
    ):
        tuple(
            BybitDemoExecutionAdapter(client).submit(
                OrderCommand(
                    command_id="33333333-3333-5333-8333-333333333333",
                    batch_id="legacy-naked-entry",
                    symbol="BUSDT",
                    side="Buy",
                    qty=0.1,
                    signed_qty=0.1,
                    reduce_only=False,
                    reference_price=10.0,
                    target_signed_qty=0.1,
                    chunk_index=0,
                    chunk_count=1,
                ),
                _market(),
            )
        )
    assert client.calls == []


def test_bybit_demo_adapter_records_only_definite_rejections() -> None:
    command = OrderCommand(
        command_id="11111111-1111-5111-8111-111111111111",
        batch_id="b1",
        symbol="BUSDT",
        side="Buy",
        qty=0.1,
        signed_qty=0.1,
        reduce_only=True,
        reference_price=10.0,
        target_signed_qty=0.0,
        chunk_index=0,
        chunk_count=1,
    )

    class RejectedClient:
        demo = True

        def place_order(self, **params: object) -> dict[str, str]:
            assert "stopLoss" not in params
            raise BybitRequestRejected("minimum notional")

    rejected = tuple(BybitDemoExecutionAdapter(RejectedClient()).submit(command, _market()))
    assert len(rejected) == 1
    assert not rejected[0].accepted
    assert rejected[0].rejection_key.endswith(":place_order_failed")

    class UncertainClient:
        demo = True

        def place_order(self, **_params: object) -> dict[str, str]:
            raise BybitSubmissionUncertain("response lost")

    with pytest.raises(BybitSubmissionUncertain, match="response lost"):
        tuple(BybitDemoExecutionAdapter(UncertainClient()).submit(command, _market()))


def test_provider_submission_attempt_is_durable_and_ambiguous_entry_never_resends(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    market = _market()
    target = _target(
        decision="ambiguous-entry",
        key="long/ambiguous/BUSDT",
        sleeve="long",
        qty=1.0,
    )
    result = kernel.submit_targets(
        batch_id="ambiguous-entry",
        market_inputs=[market],
        targets=[target],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(tick_size=0.1),
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )

    class UncertainProvider:
        name = "uncertain_provider"
        submission_outcome_can_be_ambiguous = True
        max_unsubmitted_exposure_age_ns = 5_000_000_000

        def __init__(self) -> None:
            self.submit_calls = 0

        def submit(self, command: OrderCommand, _market_input: MarketInputRef):
            self.submit_calls += 1
            durable = kernel.state().orders[command.command_id]
            assert durable.submission_attempts == 1
            assert durable.last_submission_started_ts_ns == kernel.clock.wall_time_ns()
            raise BybitSubmissionUncertain("provider may own the entry")

    adapter = UncertainProvider()
    with pytest.raises(BybitSubmissionUncertain, match="may own"):
        KernelExecutionDriver(kernel).execute_batch(
            result,
            market_inputs={"BUSDT": market},
            adapter=adapter,
        )
    command_id = result.commands[0].command_id
    attempted = kernel.state().orders[command_id]
    assert attempted.status == "commanded"
    assert attempted.submission_attempts == 1
    assert read_account_journal(tmp_path)[-1].event_type == (
        AccountEventType.SUBMISSION_ATTEMPT.value
    )

    restarted = _kernel(tmp_path)
    replayed = restarted.submit_targets(
        batch_id="ambiguous-entry",
        market_inputs=[market],
        targets=[target],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(tick_size=0.1),
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )
    with pytest.raises(AmbiguousExposureSubmission, match="refusing to resend"):
        KernelExecutionDriver(restarted).execute_batch(
            replayed,
            market_inputs={"BUSDT": market},
            adapter=adapter,
        )
    assert adapter.submit_calls == 1
    assert restarted.state().orders[command_id].submission_attempts == 1


def test_concurrent_provider_entry_submission_has_one_atomic_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    market = _market()
    result = kernel.submit_targets(
        batch_id="concurrent-provider-entry",
        market_inputs=[market],
        targets=[
            _target(
                decision="concurrent-provider-entry",
                key="long/concurrent/BUSDT",
                sleeve="long",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(tick_size=0.1),
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )

    class UncertainProvider:
        name = "concurrent_uncertain_provider"
        submission_outcome_can_be_ambiguous = True
        max_unsubmitted_exposure_age_ns = 5_000_000_000

        def __init__(self) -> None:
            self.submit_calls = 0
            self._lock = threading.Lock()

        def submit(self, _command: OrderCommand, _market_input: MarketInputRef):
            with self._lock:
                self.submit_calls += 1
            raise BybitSubmissionUncertain("provider result unknown")

    original_record_attempt = kernel.record_submission_attempt
    attempt_barrier = threading.Barrier(2)

    def synchronized_record_attempt(
        *,
        command_id: str,
        adapter_name: str,
        allow_repeat: bool = False,
    ) -> tuple[AccountEvent, ...]:
        attempt_barrier.wait(timeout=5.0)
        return original_record_attempt(
            command_id=command_id,
            adapter_name=adapter_name,
            allow_repeat=allow_repeat,
        )

    monkeypatch.setattr(
        kernel,
        "record_submission_attempt",
        synchronized_record_attempt,
    )
    adapter = UncertainProvider()
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            KernelExecutionDriver(kernel).execute_batch(
                result,
                market_inputs={"BUSDT": market},
                adapter=adapter,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=execute) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10.0)

    assert all(not worker.is_alive() for worker in workers)
    assert adapter.submit_calls == 1
    assert sum(isinstance(error, BybitSubmissionUncertain) for error in errors) == 1
    assert sum(isinstance(error, AmbiguousExposureSubmission) for error in errors) == 1
    command_id = result.commands[0].command_id
    assert kernel.state().orders[command_id].submission_attempts == 1


def test_provider_never_attempts_an_over_age_unsubmitted_entry(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    market = _market()
    result = kernel.submit_targets(
        batch_id="stale-unsubmitted-entry",
        market_inputs=[market],
        targets=[
            _target(
                decision="stale-unsubmitted-entry",
                key="long/stale/BUSDT",
                sleeve="long",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(tick_size=0.1),
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )

    class RecordingProvider:
        name = "recording_provider"
        submission_outcome_can_be_ambiguous = True
        max_unsubmitted_exposure_age_ns = 5_000_000_000

        def __init__(self) -> None:
            self.submit_calls = 0

        def submit(self, _command: OrderCommand, _market_input: MarketInputRef):
            self.submit_calls += 1
            raise AssertionError("stale entry reached provider")

    adapter = RecordingProvider()
    assert isinstance(kernel.clock, VirtualClock)
    kernel.clock.advance_ns(adapter.max_unsubmitted_exposure_age_ns + 1)
    with pytest.raises(StaleUnsubmittedExposureCommand, match="stale exposure"):
        KernelExecutionDriver(kernel).execute_batch(
            result,
            market_inputs={"BUSDT": market},
            adapter=adapter,
        )
    state = kernel.state()
    assert adapter.submit_calls == 0
    assert state.orders[result.commands[0].command_id].submission_attempts == 0
    assert read_account_journal(tmp_path)[-1].event_type == AccountEventType.ORDER_COMMAND.value


def test_provider_rechecks_entry_age_after_non_exposure_preparation(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    market = _market()
    result = kernel.submit_targets(
        batch_id="ages-during-provider-preparation",
        market_inputs=[market],
        targets=[
            _target(
                decision="ages-during-provider-preparation",
                key="long/preparation-age/BUSDT",
                sleeve="long",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(tick_size=0.1),
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )
    assert isinstance(kernel.clock, VirtualClock)

    class SlowLeverageClient:
        demo = True

        def __init__(self) -> None:
            self.order_calls = 0

        def set_leverage(self, **_params: object) -> dict[str, object]:
            kernel.clock.advance_ns(5_000_000_001)
            return {}

        def place_order(self, **_params: object) -> dict[str, str]:
            self.order_calls += 1
            raise AssertionError("stale entry reached order-create")

    client = SlowLeverageClient()
    adapter = BybitDemoExecutionAdapter(
        client,
        clock=kernel.clock,
        max_unsubmitted_exposure_age_ns=5_000_000_000,
    )
    with pytest.raises(
        StaleUnsubmittedExposureCommand,
        match="after provider preparation",
    ):
        KernelExecutionDriver(kernel).execute_batch(
            result,
            market_inputs={"BUSDT": market},
            adapter=adapter,
        )
    command_id = result.commands[0].command_id
    assert kernel.state().orders[command_id].submission_attempts == 0
    assert client.order_calls == 0


def test_sibling_latency_spends_the_shared_batch_age_budget(tmp_path: Path) -> None:
    # 2026-08-01 wedge: every slice of a chunked entry batch shares one journal
    # timestamp, each submitted slice burns real venue latency, and a budget
    # sized for one round trip refused every later slice forever. The default
    # budget must absorb whole-batch latency; a tight budget must still refuse
    # without spending an attempt.
    assert (
        BybitDemoExecutionAdapter.__init__.__kwdefaults__[
            "max_unsubmitted_exposure_age_ns"
        ]
        == 120_000_000_000
    )

    def _chunked(kernel: AccountExecutionKernel):
        market = _market()
        result = kernel.submit_targets(
            batch_id="sibling-latency-entry",
            market_inputs=[market],
            targets=[
                _target(
                    decision="sibling-latency-entry",
                    key="long/sibling-latency/BUSDT",
                    sleeve="long",
                    qty=3.0,
                )
            ],
            risk_snapshot=_snapshot(),
            risk_policy=_policy(),
            instrument_rules=_rules(tick_size=0.1, max_order_qty=1.0),
            native_protection_policy=NativeDisasterProtectionPolicy(0.2),
        )
        assert len(result.commands) == 3
        assert len({command.created_ts_ns for command in result.commands}) == 1
        return market, result

    class SlowVenueProvider:
        name = "slow_venue_provider"
        submission_outcome_can_be_ambiguous = True

        def __init__(
            self,
            kernel: AccountExecutionKernel,
            *,
            max_unsubmitted_exposure_age_ns: int,
            per_submit_ns: int,
        ) -> None:
            self.kernel = kernel
            self.max_unsubmitted_exposure_age_ns = max_unsubmitted_exposure_age_ns
            self.per_submit_ns = per_submit_ns
            self.submit_calls = 0

        def submit(self, command: OrderCommand, market_input: MarketInputRef):
            self.submit_calls += 1
            assert isinstance(self.kernel.clock, VirtualClock)
            self.kernel.clock.advance_ns(self.per_submit_ns)
            return (
                ExecutionObservation(
                    observation_type="ack",
                    command_id=command.command_id,
                    exchange_ts_ns=market_input.exchange_ts_ns,
                    local_receive_ts_ns=market_input.local_receive_ts_ns,
                    accepted=True,
                ),
            )

    kernel = _kernel(tmp_path / "wide")
    market, result = _chunked(kernel)
    adapter = SlowVenueProvider(
        kernel,
        max_unsubmitted_exposure_age_ns=120_000_000_000,
        per_submit_ns=50_000_000_000,
    )
    KernelExecutionDriver(kernel).execute_batch(
        result,
        market_inputs={"BUSDT": market},
        adapter=adapter,
    )
    assert adapter.submit_calls == 3

    wedged = _kernel(tmp_path / "tight")
    market, result = _chunked(wedged)
    tight = SlowVenueProvider(
        wedged,
        max_unsubmitted_exposure_age_ns=5_000_000_000,
        per_submit_ns=6_000_000_000,
    )
    with pytest.raises(StaleUnsubmittedExposureCommand, match="stale exposure"):
        KernelExecutionDriver(wedged).execute_batch(
            result,
            market_inputs={"BUSDT": market},
            adapter=tight,
        )
    assert tight.submit_calls == 1
    state = wedged.state()
    assert state.orders[result.commands[0].command_id].submission_attempts == 1
    assert state.orders[result.commands[1].command_id].submission_attempts == 0


def test_provider_submission_validation_precedes_attempt_and_definite_reject_is_terminal(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    market = _market()
    result = kernel.submit_targets(
        batch_id="definite-provider-reject",
        market_inputs=[market],
        targets=[
            _target(
                decision="definite-provider-reject",
                key="long/reject/BUSDT",
                sleeve="long",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(tick_size=0.1),
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )

    class RejectingProvider:
        name = "rejecting_provider"
        submission_outcome_can_be_ambiguous = True
        max_unsubmitted_exposure_age_ns = 5_000_000_000

        def __init__(self) -> None:
            self.submit_calls = 0

        def submit(self, command: OrderCommand, market_input: MarketInputRef):
            self.submit_calls += 1
            return (
                ExecutionObservation(
                    observation_type="ack",
                    command_id=command.command_id,
                    exchange_ts_ns=market_input.exchange_ts_ns,
                    local_receive_ts_ns=market_input.local_receive_ts_ns,
                    accepted=False,
                    rejection_key="provider:definite-reject",
                ),
            )

    adapter = RejectingProvider()
    driver = KernelExecutionDriver(kernel)
    with pytest.raises(ValueError, match="missing market input"):
        driver.execute_batch(result, market_inputs={}, adapter=adapter)
    command_id = result.commands[0].command_id
    assert kernel.state().orders[command_id].submission_attempts == 0

    driver.execute_batch(
        result,
        market_inputs={"BUSDT": market},
        adapter=adapter,
    )
    rejected = kernel.state().orders[command_id]
    assert rejected.submission_attempts == 1
    assert rejected.status == "rejected"
    driver.execute_batch(
        result,
        market_inputs={"BUSDT": market},
        adapter=adapter,
    )
    assert adapter.submit_calls == 1


def test_provider_may_retry_ambiguous_reduce_only_command(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    market = _book().market_ref(input_key="reduce-retry-book")
    opened = kernel.submit_targets(
        batch_id="reduce-retry-open",
        market_inputs=[market],
        targets=[
            _target(
                decision="reduce-retry-open",
                key="long/reduce-retry/BUSDT",
                sleeve="long",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    KernelExecutionDriver(kernel).execute_batch(
        opened,
        market_inputs={"BUSDT": market},
        adapter=_twin(name="paper"),
    )
    closing = kernel.submit_targets(
        batch_id="reduce-retry-close",
        market_inputs=[market],
        targets=[
            _target(
                decision="reduce-retry-close",
                key="long/reduce-retry/BUSDT",
                sleeve="long",
                qty=0.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    assert closing.commands[0].reduce_only

    class UncertainReductionProvider:
        name = "uncertain_reduction_provider"
        submission_outcome_can_be_ambiguous = True
        max_unsubmitted_exposure_age_ns = 1

        def __init__(self) -> None:
            self.submit_calls = 0

        def submit(self, _command: OrderCommand, _market_input: MarketInputRef):
            self.submit_calls += 1
            raise BybitSubmissionUncertain("reduction response lost")

    adapter = UncertainReductionProvider()
    driver = KernelExecutionDriver(kernel)
    for expected_attempt in (1, 2):
        with pytest.raises(BybitSubmissionUncertain, match="response lost"):
            driver.execute_batch(
                closing,
                market_inputs={"BUSDT": market},
                adapter=adapter,
            )
        order = kernel.state().orders[closing.commands[0].command_id]
        assert order.submission_attempts == expected_attempt
    assert adapter.submit_calls == 2


def test_dropped_ack_reordered_duplicate_fills_recover_deterministically(tmp_path: Path) -> None:
    def run(root: Path) -> list[dict[str, object]]:
        kernel = _kernel(root, account_id="fault-account")
        market = _book(ask_qty=1.0).market_ref(input_key="fault-book")
        result = kernel.submit_targets(
            batch_id="fault-batch",
            market_inputs=[market],
            targets=[_target(decision="d1", key="long/main/BUSDT", sleeve="long", qty=2.0)],
            risk_snapshot=_snapshot(),
            risk_policy=_policy(),
            instrument_rules=_rules(),
        )
        raw_observations = tuple(_twin(name="fault-source", book=_book(ask_qty=1.0)).submit(result.commands[0], market))
        # Simulate a lost create ack plus reordered, duplicate private fills.
        fills = [
            asdict(observation)
            for observation in reversed(raw_observations)
            if str(observation.observation_type) == "fill"
        ]
        deliveries = fills + list(reversed(fills))
        driver = KernelExecutionDriver(kernel)
        for delivery in deliveries:
            driver.ingest([delivery])
        state = kernel.state()
        assert state.positions["BUSDT"].signed_qty == pytest.approx(2.0)
        assert len(state.executions) == 2
        ack = next(event for event in read_account_journal(root) if event.event_type == AccountEventType.ACK.value)
        assert ack.payload["metadata"]["inferred_from_execution_id"]
        return [event.to_dict() for event in read_account_journal(root)]

    assert run(tmp_path / "one") == run(tmp_path / "two")


def test_sleeve_adapters_only_propose_targets_and_runtime_nets_them_once(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    market = _market()
    runtime = AccountKernelRuntime(kernel)
    result = runtime.process_cycle(
        batch_id="all-sleeves",
        intents=[
            AdaptedIntent(
                LongTargetAdapter(),
                SleeveTargetIntent(
                    decision_key="long-d",
                    target_key="long/main/BUSDT",
                    strategy_id="long-v1",
                    component_id="main",
                    symbol="BUSDT",
                    signed_notional_usdt=20.0,
                    leverage=10.0,
                    reason="long entry",
                ),
            ),
            AdaptedIntent(
                ContinuousTargetAdapter(),
                SleeveTargetIntent(
                    decision_key="continuous-d",
                    target_key="continuous/main/BUSDT",
                    strategy_id="continuous-v1",
                    component_id="main",
                    symbol="BUSDT",
                    signed_notional_usdt=-50.0,
                    leverage=10.0,
                    reason="continuous entry",
                ),
            ),
            AdaptedIntent(
                HedgeTargetAdapter(),
                SleeveTargetIntent(
                    decision_key="hedge-d",
                    target_key="hedge/main/BUSDT",
                    strategy_id="hedge-v1",
                    component_id="main",
                    symbol="BUSDT",
                    signed_notional_usdt=10.0,
                    leverage=10.0,
                    reason="portfolio hedge",
                ),
            ),
        ],
        market_inputs={"BUSDT": market},
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
        execution_adapter=None,
    )
    assert result.execution_events == ()
    assert result.target_result.accepted
    assert len(result.target_result.commands) == 1
    assert result.target_result.commands[0].signed_qty == pytest.approx(-2.0)
    assert kernel.state().positions == {}  # adapters cannot invent fills or mutate the venue state


def test_risk_adapter_replaces_owned_target_with_zero_instead_of_submitting_reduce_order(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    runtime = AccountKernelRuntime(kernel)
    market = _market()
    opened = runtime.process_cycle(
        batch_id="open-via-adapter",
        intents=[
            AdaptedIntent(
                ContinuousTargetAdapter(),
                SleeveTargetIntent(
                    decision_key="open-d",
                    target_key="continuous/main/BUSDT",
                    strategy_id="continuous-v1",
                    component_id="main",
                    symbol="BUSDT",
                    signed_notional_usdt=-20.0,
                    leverage=10.0,
                    reason="entry",
                ),
            )
        ],
        market_inputs={"BUSDT": market},
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
        execution_adapter=_twin(name="paper"),
    )
    assert opened.target_result.accepted
    assert kernel.state().positions["BUSDT"].signed_qty == pytest.approx(-2.0)

    closed = runtime.process_cycle(
        batch_id="risk-exit-via-target",
        intents=[
            AdaptedIntent(
                RiskTargetAdapter(),
                SleeveTargetIntent(
                    decision_key="risk-d",
                    target_key="continuous/main/BUSDT",
                    strategy_id="account-risk-v1",
                    component_id="main",
                    symbol="BUSDT",
                    signed_notional_usdt=0.0,
                    leverage=10.0,
                    reason="max hold",
                ),
            )
        ],
        market_inputs={"BUSDT": market},
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
        execution_adapter=_twin(name="paper"),
    )
    assert closed.target_result.commands[0].reduce_only
    assert kernel.state().positions["BUSDT"].signed_qty == 0.0


def test_directional_adapters_reject_wrong_side_before_account_mutation() -> None:
    bad = SleeveTargetIntent(
        decision_key="bad",
        target_key="long/main/BUSDT",
        strategy_id="long-v1",
        component_id="main",
        symbol="BUSDT",
        signed_notional_usdt=-10.0,
        leverage=10.0,
        reason="invalid",
    )
    with pytest.raises(ValueError, match="refuses a short target"):
        LongTargetAdapter().desired_target(bad, _market(), _rules()["BUSDT"])


def test_notional_adapter_rounds_quantity_to_venue_step_without_hidden_notional_floor() -> None:
    market = _market(price=9.95)
    intent = SleeveTargetIntent(
        decision_key="rounding",
        target_key="long/main/BUSDT",
        strategy_id="long-v1",
        component_id="main",
        symbol="BUSDT",
        signed_notional_usdt=20.0,
        leverage=10.0,
        reason="rounding",
    )
    target = LongTargetAdapter().desired_target(intent, market, _rules()["BUSDT"])
    assert target.signed_qty == pytest.approx(2.0)
    assert target.metadata["raw_signed_qty"] == pytest.approx(20.0 / 9.95)
    assert target.metadata["quantity_rounding"] == "toward_zero_to_venue_step"


def test_chunked_commands_carry_exact_step_quantities(tmp_path: Path) -> None:
    """Float chunk subtraction must not leak binary dust into command qty.

    2.7 split into 1.0-unit chunks leaves 0.7000000000000002 under float
    accumulation; the adapter transmits qty verbatim and the venue rejects an
    off-step quantity, wedging the final chunk of a close.
    """

    kernel = _kernel(tmp_path)
    rules = _rules(max_order_qty=1.0)
    market = _book().market_ref(input_key="dust-book")
    result = kernel.submit_targets(
        batch_id="dust-open",
        market_inputs=[market],
        targets=[
            _target(
                decision="dust-d",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=2.7,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
    )
    assert result.accepted
    quantities = [command.qty for command in result.commands]
    assert quantities == [1.0, 1.0, 0.7]
    from decimal import Decimal as _Decimal

    assert format(_Decimal(str(quantities[-1])), "f") == "0.7"


def test_native_entry_stop_is_durable_on_every_chunk_and_crash_replay(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    rules = _rules(max_order_qty=1.0, tick_size=0.1)
    market = _market(price=10.0)
    target = _target(
        decision="protected-entry",
        key="long/protected/BUSDT",
        sleeve="long",
        qty=2.7,
        metadata={"stop_loss_pct": 0.125},
    )
    result = kernel.submit_targets(
        batch_id="protected-entry",
        market_inputs=[market],
        targets=[target],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )

    assert result.accepted
    assert len(result.commands) == 3
    assert {command.entry_stop_price for command in result.commands} == {8.7}
    assert {command.entry_stop_fraction for command in result.commands} == {0.125}
    assert {command.entry_stop_source for command in result.commands} == {
        "decision_reference_outermost_component_fraction"
    }
    assert {command.entry_stop_trigger_by for command in result.commands} == {"MarkPrice"}
    reconstructed = kernel.state()
    assert {reconstructed.orders[command.command_id].entry_stop_price for command in result.commands} == {8.7}
    risk = reconstructed.risk_decisions["protected-entry"]
    assert risk["native_disaster_protection_policy"] == {
        "fallback_stop_fraction": 0.2,
        "trigger_by": "MarkPrice",
    }

    # A crash retry recovers the originally journaled stop even if runtime
    # configuration changed after the command boundary.
    replayed = kernel.submit_targets(
        batch_id="protected-entry",
        market_inputs=[market],
        targets=[target],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=NativeDisasterProtectionPolicy(0.3),
    )
    assert [command.entry_stop_price for command in replayed.commands] == [
        8.7,
        8.7,
        8.7,
    ]


@pytest.mark.parametrize(
    ("protection_fields", "error"),
    (
        ({"entry_stop_fraction": 0.2}, "entry_stop_price is required"),
        (
            {
                "entry_stop_price": 8.0,
                "entry_stop_source": "test",
                "entry_stop_trigger_by": "MarkPrice",
            },
            "entry_stop_fraction must be in",
        ),
        (
            {
                "entry_stop_price": 11.0,
                "entry_stop_fraction": 0.2,
                "entry_stop_source": "test",
                "entry_stop_trigger_by": "MarkPrice",
            },
            "stop must be below",
        ),
    ),
)
def test_order_event_replay_rejects_partial_or_crossed_entry_protection(
    protection_fields: dict[str, object],
    error: str,
) -> None:
    payload = {
        "command_id": "protected-replay-command",
        "batch_id": "protected-replay-batch",
        "signed_qty": 1.0,
        "reduce_only": False,
        "reference_price": 10.0,
        "created_ts_ns": 100,
        **protection_fields,
    }
    event = AccountEvent(
        schema_version=1,
        event_id="protected-replay-event",
        sequence=1,
        event_type=AccountEventType.ORDER_COMMAND.value,
        correlation_id="protected-replay-batch",
        causation_id="protected-replay-batch",
        account_id="demo",
        sleeve="account_execution",
        symbol="BUSDT",
        wall_ts_ns=100,
        monotonic_ns=100,
        payload=payload,
        prev_event_hash="",
        state_hash="",
        event_hash="",
    )

    with pytest.raises(account_kernel_module.AccountTransitionError, match=error):
        account_kernel_module.apply_account_event(
            account_kernel_module.AccountState(),
            event,
        )


def test_native_entry_stop_uses_outermost_component_or_account_fallback(
    tmp_path: Path,
) -> None:
    rules = _rules(tick_size=0.1)
    policy = NativeDisasterProtectionPolicy(0.3)
    explicit = _kernel(tmp_path / "explicit").submit_targets(
        batch_id="outermost",
        market_inputs=[_market(price=10.0)],
        targets=[
            _target(
                decision="inner",
                key="long/inner/BUSDT",
                sleeve="long",
                qty=1.0,
                metadata={"stop_loss_pct": 0.1},
            ),
            _target(
                decision="outer",
                key="long/outer/BUSDT",
                sleeve="long",
                qty=1.0,
                metadata={"stop_loss_pct": 0.2},
            ),
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=policy,
    )
    assert explicit.accepted
    assert explicit.commands[0].entry_stop_price == 8.0
    assert explicit.commands[0].entry_stop_fraction == 0.2

    fallback = _kernel(tmp_path / "fallback").submit_targets(
        batch_id="fallback",
        market_inputs=[_market(price=10.0)],
        targets=[
            _target(
                decision="fallback",
                key="long/fallback/BUSDT",
                sleeve="long",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=policy,
    )
    assert fallback.accepted
    assert fallback.commands[0].entry_stop_price == 7.0
    assert fallback.commands[0].entry_stop_source == ("decision_reference_account_fallback_fraction")


@pytest.mark.parametrize(
    ("signed_qty", "fill_price", "active_stop", "scale_price"),
    (
        (1.0, 9.0, 7.2, 20.0),
        (-1.0, 11.0, 13.2, 5.0),
    ),
)
def test_native_scale_in_preserves_outer_existing_fill_anchored_stop(
    tmp_path: Path,
    signed_qty: float,
    fill_price: float,
    active_stop: float,
    scale_price: float,
) -> None:
    kernel = _kernel(tmp_path)
    rules = _rules(tick_size=0.1)
    policy = NativeDisasterProtectionPolicy(0.2)
    owner_sleeve = "long" if signed_qty > 0.0 else "continuous"
    owner_key = f"{owner_sleeve}/existing/BUSDT"
    opened = kernel.submit_targets(
        batch_id="protected-existing-open",
        market_inputs=[_market(price=10.0)],
        targets=[
            _target(
                decision="protected-existing-open",
                key=owner_key,
                sleeve=owner_sleeve,
                qty=signed_qty,
                metadata={"stop_loss_pct": 0.2},
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=policy,
    )
    command = opened.commands[0]
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=command.command_id,
                exchange_ts_ns=1_010_000_000,
                local_receive_ts_ns=1_020_000_000,
                accepted=True,
                venue_order_id="existing-entry",
            ),
            ExecutionObservation(
                observation_type="fill",
                command_id=command.command_id,
                exchange_ts_ns=1_030_000_000,
                local_receive_ts_ns=1_040_000_000,
                venue_order_id="existing-entry",
                execution_id="existing-entry-fill",
                signed_qty=signed_qty,
                price=fill_price,
                fee_usdt=0.01,
            ),
        )
    )
    kernel.record_protection(
        protection_key="native-existing-exact",
        symbol="BUSDT",
        status="active",
        stop_price=active_stop,
        take_profit_price=None,
        exchange_ts_ns=1_050_000_000,
        local_receive_ts_ns=1_060_000_000,
        metadata={
            "native_exchange": True,
            "symbol": "BUSDT",
            "signed_qty": signed_qty,
            "trigger_by": "MarkPrice",
        },
    )

    scaled = kernel.submit_targets(
        batch_id="protected-scale-in",
        market_inputs=[_market(price=scale_price, key="scale-book")],
        targets=[
            _target(
                decision="protected-scale-in",
                key=f"{owner_sleeve}/new/BUSDT",
                sleeve=owner_sleeve,
                qty=signed_qty,
                price=scale_price,
                metadata={"stop_loss_pct": 0.1},
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=policy,
    )

    assert scaled.accepted
    assert scaled.commands[0].signed_qty == pytest.approx(signed_qty)
    assert scaled.commands[0].entry_stop_price == pytest.approx(active_stop)
    assert scaled.commands[0].entry_stop_source.endswith(
        "_clamped_to_existing_native_stop"
    )


def test_native_scale_in_requires_existing_open_position_protection(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    rules = _rules(tick_size=0.1)
    policy = NativeDisasterProtectionPolicy(0.2)
    opened = kernel.submit_targets(
        batch_id="unprotected-existing-open",
        market_inputs=[_market()],
        targets=[
            _target(
                decision="unprotected-existing-open",
                key="long/existing/BUSDT",
                sleeve="long",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=policy,
    )
    command = opened.commands[0]
    KernelExecutionDriver(kernel).ingest(
        (
            ExecutionObservation(
                observation_type="ack",
                command_id=command.command_id,
                exchange_ts_ns=1_010_000_000,
                local_receive_ts_ns=1_020_000_000,
                accepted=True,
                venue_order_id="unprotected-entry",
            ),
            ExecutionObservation(
                observation_type="fill",
                command_id=command.command_id,
                exchange_ts_ns=1_030_000_000,
                local_receive_ts_ns=1_040_000_000,
                venue_order_id="unprotected-entry",
                execution_id="unprotected-entry-fill",
                signed_qty=1.0,
                price=10.0,
                fee_usdt=0.01,
            ),
        )
    )

    scale = kernel.submit_targets(
        batch_id="unprotected-scale-in",
        market_inputs=[_market(key="scale-book")],
        targets=[
            _target(
                decision="unprotected-scale-in",
                key="long/new/BUSDT",
                sleeve="long",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=policy,
    )
    assert not scale.accepted
    assert scale.commands == ()
    assert scale.rejection_keys == (
        "account-risk:unprotected-scale-in:"
        "native_entry_protection_missing_existing_native_protection:BUSDT",
    )


def test_reduce_only_command_never_carries_entry_attached_protection(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    rules = _rules(tick_size=0.1)
    policy = NativeDisasterProtectionPolicy(0.2)
    target_key = "long/reduction/BUSDT"
    opened = kernel.submit_targets(
        batch_id="protected-before-reduction",
        market_inputs=[_market()],
        targets=[
            _target(
                decision="protected-before-reduction",
                key=target_key,
                sleeve="long",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=policy,
    )
    KernelExecutionDriver(kernel).execute_batch(
        opened,
        market_inputs={"BUSDT": _book().market_ref(input_key="reduction-open-book")},
        adapter=_twin(name="paper"),
    )
    reduced = kernel.submit_targets(
        batch_id="protected-reduction",
        market_inputs=[_market(key="reduction-book")],
        targets=[
            _target(
                decision="protected-reduction",
                key=target_key,
                sleeve="long",
                qty=0.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
        native_protection_policy=policy,
    )
    command = reduced.commands[0]
    assert command.reduce_only
    assert command.entry_stop_price is None
    assert command.entry_stop_fraction is None
    assert command.entry_stop_source == ""
    assert command.entry_stop_trigger_by == ""


@pytest.mark.parametrize("invalid", [0.0, 1.0, -0.1, "bad", True])
def test_invalid_component_stop_blocks_native_entry_before_command(
    tmp_path: Path,
    invalid: object,
) -> None:
    result = _kernel(tmp_path).submit_targets(
        batch_id=f"invalid-stop-{invalid!s}",
        market_inputs=[_market(price=10.0)],
        targets=[
            _target(
                decision="invalid-stop",
                key="long/invalid/BUSDT",
                sleeve="long",
                qty=1.0,
                metadata={"stop_loss_pct": invalid},
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(tick_size=0.1),
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
    )
    assert not result.accepted
    assert result.commands == ()
    assert result.rejection_keys == (
        f"account-risk:invalid-stop-{invalid!s}:native_entry_protection_invalid_stop_fraction:BUSDT",
    )


def test_duplicate_terminal_status_from_second_consumer_is_idempotent(
    tmp_path: Path,
) -> None:
    """WS and REST recovery race the same terminal fact with different local
    timestamps; the second commit must be a no-op, not an integrity error."""

    kernel = _kernel(tmp_path)
    rules = _rules()
    market = _book().market_ref(input_key="terminal-book")
    result = kernel.submit_targets(
        batch_id="terminal-race",
        market_inputs=[market],
        targets=[
            _target(
                decision="terminal-race-d",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
    )
    assert result.accepted and len(result.commands) == 1
    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-terminal-race",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_201_000_000,
    )
    kernel.record_fill(
        command_id=command.command_id,
        execution_id="fill-terminal-race",
        signed_qty=1.0,
        price=10.0,
        fee_usdt=0.0,
        exchange_ts_ns=1_202_000_000,
        local_receive_ts_ns=1_203_000_000,
    )

    first = kernel.record_order_status(
        command_id=command.command_id,
        status="filled",
        cumulative_filled_qty=1.0,
        exchange_ts_ns=1_204_000_000,
        local_receive_ts_ns=1_205_000_000,
    )
    assert len(first) == 1

    second = kernel.record_order_status(
        command_id=command.command_id,
        status="filled",
        cumulative_filled_qty=1.0,
        exchange_ts_ns=1_204_000_000,
        local_receive_ts_ns=1_299_000_000,
    )
    assert second == ()


def _sub_cent_book() -> L2BookSnapshot:
    return L2BookSnapshot(
        symbol="BUSDT",
        sequence=100,
        previous_sequence=99,
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        bids=(BookLevel(0.00099, 5_000_000.0),),
        asks=(BookLevel(0.00101, 5_000_000.0),),
        sequence_gap=False,
        clock_offset_estimate_ns=100_000_000,
    )


def test_large_multi_fill_order_reaches_filled_without_wedging_reconciliation(
    tmp_path: Path,
) -> None:
    """Terminal ORDER_STATUS and fill reconstruction must share a quantity-scaled
    tolerance. A five-figure USDT position in a sub-cent coin is 1e6 base units;
    filling it in several partials accumulates ~1e-10 of float error, and an
    absolute 1e-12 check raises inside the journal transaction on every retry.
    """

    kernel = _kernel(tmp_path)
    rules = {
        "BUSDT": InstrumentRules(
            symbol="BUSDT",
            qty_step=0.1,
            min_qty=0.1,
            min_notional=1.0,
            tick_size=0.0,
            max_order_qty=5_000_000.0,
            max_leverage=20.0,
        )
    }
    policy = AccountRiskPolicy(
        max_component_gross_notional_usdt=5_000.0,
        max_account_gross_notional_usdt=5_000.0,
        max_symbol_notional_usdt=5_000.0,
        max_initial_margin_usdt=1_000.0,
        max_leverage=10.0,
    )
    market = _sub_cent_book().market_ref(input_key="sub-cent-book")
    result = kernel.submit_targets(
        batch_id="large-qty",
        market_inputs=[market],
        targets=[
            _target(
                decision="large-qty-d",
                key="continuous/main/BUSDT",
                sleeve="continuous",
                qty=1_000_000.0,
                price=0.001,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=policy,
        instrument_rules=rules,
    )
    assert result.accepted and len(result.commands) == 1
    command = result.commands[0]
    assert command.signed_qty == 1_000_000.0
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-large-qty",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_201_000_000,
    )

    # Partials whose float sum is 1e-10 short of the commanded quantity.
    partials = (865_525.6, 63_572.2, 70_902.2)
    naive_total = 0.0
    for quantity in partials:
        naive_total += quantity
    assert naive_total != 1_000_000.0
    for index, quantity in enumerate(partials):
        kernel.record_fill(
            command_id=command.command_id,
            execution_id=f"fill-large-{index}",
            signed_qty=quantity,
            price=0.001,
            fee_usdt=0.0,
            exchange_ts_ns=1_202_000_000 + index,
            local_receive_ts_ns=1_203_000_000 + index,
        )

    order = kernel._state_ref().orders[command.command_id]
    assert order.status == "filled"
    # Snapped, so no residual drift can reach a downstream comparison.
    assert order.filled_signed_qty == command.signed_qty
    assert order.remaining_signed_qty == 0.0

    # The venue's terminal row must commit rather than raise inside the journal
    # transaction; a raise here latches owner health BLOCKED forever.
    kernel.record_order_status(
        command_id=command.command_id,
        status="filled",
        cumulative_filled_qty=1_000_000.0,
        exchange_ts_ns=1_204_000_000,
        local_receive_ts_ns=1_205_000_000,
    )
    assert kernel._state_ref().orders[command.command_id].terminal_status_recorded


def test_quantity_tolerance_scales_with_the_quantity_being_compared() -> None:
    assert quantity_tolerance(0.0) == 1e-12
    assert quantity_tolerance(1.0) == 1e-12
    assert quantity_tolerance(-1_000_000.0) == pytest.approx(1e-6)


def _journal_snapshot(kernel: AccountExecutionKernel, key: str) -> None:
    kernel.record_venue_snapshot(
        snapshot_key=key,
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=1,
        local_receive_ts_ns=2,
    )


def test_read_recent_account_events_reads_only_the_tail_window(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    for index in range(5):
        _journal_snapshot(kernel, f"recent-{index}")
    full = read_account_journal(tmp_path)
    recent = account_kernel_module.read_recent_account_events(tmp_path, max_segments=2)
    assert recent is not None
    assert recent.total_segments == 5
    assert recent.window_segments == 2
    assert list(recent.events) == full[-len(recent.events) :]
    assert recent.events[-1] == full[-1]


def test_read_recent_account_events_covers_a_small_journal_whole(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    for index in range(3):
        _journal_snapshot(kernel, f"whole-{index}")
    recent = account_kernel_module.read_recent_account_events(tmp_path, max_segments=100)
    assert recent is not None
    assert recent.total_segments == recent.window_segments == 3
    assert list(recent.events) == read_account_journal(tmp_path)


def test_read_recent_account_events_empty_journal_is_none(tmp_path: Path) -> None:
    assert account_kernel_module.read_recent_account_events(tmp_path, max_segments=8) is None


def test_read_recent_account_events_rejects_a_tampered_window_segment(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    _journal_snapshot(kernel, "tamper-0")
    _journal_snapshot(kernel, "tamper-1")
    latest = sorted(account_transactions_path(tmp_path).glob("*.json"))[-1]
    latest.write_bytes(latest.read_bytes().replace(b"tamper-1", b"tamper-x"))
    with pytest.raises(AccountJournalIntegrityError):
        account_kernel_module.read_recent_account_events(tmp_path, max_segments=2)


def test_read_recent_account_events_rejects_a_cross_segment_chain_break(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    kernel_a = _kernel(root_a)
    _journal_snapshot(kernel_a, "chain-a-0")
    kernel_b = _kernel(root_b)
    _journal_snapshot(kernel_b, "chain-b-0")
    _journal_snapshot(kernel_b, "chain-b-1")
    foreign = sorted(account_transactions_path(root_b).glob("*.json"))[-1]
    (account_transactions_path(root_a) / foreign.name).write_bytes(foreign.read_bytes())
    with pytest.raises(AccountJournalIntegrityError, match="hash-chain break"):
        account_kernel_module.read_recent_account_events(root_a, max_segments=10)


def test_validated_names_cache_revalidates_only_the_new_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _kernel(tmp_path)
    for index in range(4):
        _journal_snapshot(kernel, f"cache-{index}")
    validated_lengths: list[int] = []
    original = account_kernel_module._contiguous_from

    def observing(names, *, expected_first):  # type: ignore[no-untyped-def]
        validated_lengths.append(len(tuple(names)))
        return original(names, expected_first=expected_first)

    monkeypatch.setattr(account_kernel_module, "_contiguous_from", observing)
    assert read_account_journal_head(tmp_path) is not None
    assert validated_lengths[-1] == 4
    _journal_snapshot(kernel, "cache-4")
    assert read_account_journal_head(tmp_path) is not None
    assert validated_lengths[-1] == 1


def test_validated_names_cache_survives_a_root_reset(tmp_path: Path) -> None:
    import shutil

    kernel = _kernel(tmp_path)
    _journal_snapshot(kernel, "reset-0")
    _journal_snapshot(kernel, "reset-1")
    assert read_account_journal_head(tmp_path) is not None
    shutil.rmtree(account_transactions_path(tmp_path))
    account_journal_path(tmp_path).unlink(missing_ok=True)
    fresh = _kernel(tmp_path)
    _journal_snapshot(fresh, "reset-fresh")
    head = read_account_journal_head(tmp_path)
    assert head is not None
    assert head.payload.get("snapshot_key") == "reset-fresh"


def test_sparse_venue_snapshots_replay_to_the_same_domain_state(tmp_path: Path) -> None:
    """Ten-minute checkpoints reconstruct the same book as 30-second ones.

    The reducer keeps only the newest snapshot and no production code reads
    ``state.venue_snapshots``, so dropping heartbeat checkpoints changes the
    journal's length and its rolling hash but nothing a consumer trades on.
    """

    def run(root: Path, *, snapshot_every_step: bool) -> account_kernel_module.AccountState:
        kernel = _kernel(root)
        driver = KernelExecutionDriver(kernel)
        snapshot_index = 0

        def checkpoint(always: bool = False) -> None:
            nonlocal snapshot_index
            if not (snapshot_every_step or always):
                return
            snapshot_index += 1
            kernel.record_venue_snapshot(
                snapshot_key=f"snapshot-{snapshot_index}",
                venue_positions={},
                reconstructed_positions={},
                mismatches=(),
                exchange_ts_ns=0,
                local_receive_ts_ns=1_000_000_000 + snapshot_index,
            )

        checkpoint()
        market = _book().market_ref(input_key="open-book")
        opened = kernel.submit_targets(
            batch_id="open",
            market_inputs=[market],
            targets=[_target(decision="open-d", key="long/main/BUSDT", sleeve="long", qty=2.0)],
            risk_snapshot=_snapshot(),
            risk_policy=_policy(),
            instrument_rules=_rules(),
        )
        checkpoint()
        driver.execute_batch(opened, market_inputs={"BUSDT": market}, adapter=_twin(name="paper"))
        checkpoint()
        kernel.record_protection(
            protection_key="BUSDT-protection-v1",
            symbol="BUSDT",
            status="active",
            stop_price=9.0,
            take_profit_price=12.0,
            exchange_ts_ns=1_030_000_000,
            local_receive_ts_ns=1_035_000_000,
            command_id=opened.commands[0].command_id,
        )
        checkpoint()
        close_market = _book().market_ref(input_key="close-book")
        closing = kernel.submit_targets(
            batch_id="close",
            market_inputs=[close_market],
            targets=[_target(decision="close-d", key="long/main/BUSDT", sleeve="long", qty=0.0)],
            risk_snapshot=_snapshot(),
            risk_policy=_policy(),
            instrument_rules=_rules(),
        )
        checkpoint()
        driver.execute_batch(
            closing, market_inputs={"BUSDT": close_market}, adapter=_twin(name="paper")
        )
        kernel.record_close(
            close_key="BUSDT-close-1",
            symbol="BUSDT",
            reason="take_profit",
            venue_flat=True,
            exchange_ts_ns=1_100_000_000,
            local_receive_ts_ns=1_105_000_000,
            command_id=closing.commands[0].command_id,
        )
        kernel.record_pnl(
            pnl_key="BUSDT-pnl-1",
            close_key="BUSDT-close-1",
            symbol="BUSDT",
            gross_pnl_usdt=-0.4,
            fee_usdt=0.022,
            funding_usdt=0.0,
            net_pnl_usdt=-0.422,
            exchange_ts_ns=1_110_000_000,
            local_receive_ts_ns=1_115_000_000,
            source="venue_closed_pnl",
        )
        # Both books end on a checkpoint, which is what a consumer reads.
        checkpoint(always=True)
        return account_kernel_module.reduce_account_events(
            read_account_journal(root, verify=True)
        )

    dense = run(tmp_path / "dense", snapshot_every_step=True)
    sparse = run(tmp_path / "sparse", snapshot_every_step=False)

    assert dense.events_applied > sparse.events_applied
    assert {symbol: position.signed_qty for symbol, position in dense.positions.items()} == {
        symbol: position.signed_qty for symbol, position in sparse.positions.items()
    }
    assert {symbol: asdict(position) for symbol, position in dense.positions.items()} == {
        symbol: asdict(position) for symbol, position in sparse.positions.items()
    }
    assert dense.aggregate_targets == sparse.aggregate_targets
    assert sorted(dense.orders) == sorted(sparse.orders)

    def order_rows(state: account_kernel_module.AccountState) -> list[dict[str, object]]:
        rows = []
        for key in sorted(state.orders):
            row = asdict(state.orders[key])
            # Where an order sits in the journal, not what it is.
            row.pop("command_sequence")
            rows.append(row)
        return rows

    assert order_rows(dense) == order_rows(sparse)
    assert dense.closes == sparse.closes
    assert dense.pnl == sparse.pnl
    # Journal position is the only thing that moved: the chained hash and the
    # per-order sequence number. Neither is a traded quantity.
    assert dense.rolling_state_hash != sparse.rolling_state_hash
    assert [dense.orders[key].command_sequence for key in sorted(dense.orders)] != [
        sparse.orders[key].command_sequence for key in sorted(sparse.orders)
    ]


def _indexed_journal(root: Path) -> AccountExecutionKernel:
    """Open, partially chunk, and close a symbol so both indexes hold rows."""

    kernel = _kernel(root)
    rules = _rules(max_order_qty=1.0)
    market = _book().market_ref(input_key="index-book")
    driver = KernelExecutionDriver(kernel)
    open_result = kernel.submit_targets(
        batch_id="index-open",
        market_inputs=[market],
        targets=[
            _target(decision="index-open-d", key="continuous/main/BUSDT", sleeve="continuous", qty=2.0)
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
    )
    driver.execute_batch(
        open_result,
        market_inputs={"BUSDT": market},
        adapter=_twin(name="historical", rules=rules),
    )
    add_result = kernel.submit_targets(
        batch_id="index-add",
        market_inputs=[market],
        targets=[
            _target(decision="index-add-d", key="long/main/BUSDT", sleeve="long", qty=1.0),
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
    )
    driver.execute_batch(
        add_result,
        market_inputs={"BUSDT": market},
        adapter=_twin(name="historical", rules=rules),
    )
    close_result = kernel.submit_targets(
        batch_id="index-close",
        market_inputs=[market],
        targets=[
            _target(decision="index-close-d", key="continuous/main/BUSDT", sleeve="continuous", qty=0.0),
            _target(decision="index-close-long-d", key="long/main/BUSDT", sleeve="long", qty=0.0),
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=rules,
    )
    driver.execute_batch(
        close_result,
        market_inputs={"BUSDT": market},
        adapter=_twin(name="historical", rules=rules),
    )
    return kernel


def _scanned_command_ids_by_venue_order_id(state: AccountState) -> dict[str, set[str]]:
    scanned: dict[str, set[str]] = {}
    for order in state.orders.values():
        if order.venue_order_id:
            scanned.setdefault(order.venue_order_id, set()).add(order.command_id)
    return scanned


def _scanned_target_proposal_keys_by_batch(state: AccountState) -> dict[str, set[str]]:
    scanned: dict[str, set[str]] = {}
    for proposal_key, target in state.target_proposals.items():
        scanned.setdefault(str(target.get("batch_id") or ""), set()).add(proposal_key)
    return scanned


def test_reducer_indexes_equal_a_brute_force_scan_of_the_state_they_accelerate(
    tmp_path: Path,
) -> None:
    kernel = _indexed_journal(tmp_path)
    state = kernel.state()

    venue_scan = _scanned_command_ids_by_venue_order_id(state)
    batch_scan = _scanned_target_proposal_keys_by_batch(state)
    # Not vacuous: several acknowledged commands and three batches of targets.
    assert len(venue_scan) >= 4
    assert set(batch_scan) == {"index-open", "index-add", "index-close"}
    assert state.command_ids_by_venue_order_id == venue_scan
    assert state.target_proposal_keys_by_batch == batch_scan

    # A replay from the durable segments rebuilds the same indexes, and
    # ``verify=True`` re-checks every event's state hash on the way through:
    # the indexes are reducer bookkeeping and change no recorded hash.
    replayed = account_kernel_module.reduce_account_events(read_account_journal(tmp_path, verify=True))
    assert replayed.command_ids_by_venue_order_id == venue_scan
    assert replayed.target_proposal_keys_by_batch == batch_scan
    receipt = verify_account_journal(tmp_path)
    assert receipt["events"] == len(read_account_journal(tmp_path))
    assert receipt["final_state_hash"] == state.state_hash()


def test_transaction_state_copy_shares_index_sets_and_the_reducer_rebinds(
    tmp_path: Path,
) -> None:
    """The copy shares the index sets; the reducer must never mutate one.

    Copying every set per journal write cost a slice of the whole account
    history under the journal lock. Sharing is only safe because the reducer
    rebinds (``d[key] = {*d.get(key, ()), value}``); an in-place ``.add`` would
    reach through into committed state and a rolled-back transaction would
    leave the committed index carrying events that never landed.
    """

    committed = _indexed_journal(tmp_path).state()
    copied = transaction_state_copy(committed)
    assert copied.command_ids_by_venue_order_id == committed.command_ids_by_venue_order_id
    assert copied.target_proposal_keys_by_batch == committed.target_proposal_keys_by_batch

    venue_order_id = sorted(committed.command_ids_by_venue_order_id)[0]
    batch_id = sorted(committed.target_proposal_keys_by_batch)[0]
    before_venue = set(committed.command_ids_by_venue_order_id[venue_order_id])
    before_batch = set(committed.target_proposal_keys_by_batch[batch_id])

    # Exactly what the reducer does.
    copied.command_ids_by_venue_order_id[venue_order_id] = {
        *copied.command_ids_by_venue_order_id.get(venue_order_id, ()),
        "abandoned-command",
    }
    copied.command_ids_by_venue_order_id["abandoned-venue-order"] = {"abandoned-command"}
    copied.target_proposal_keys_by_batch[batch_id] = {
        *copied.target_proposal_keys_by_batch.get(batch_id, ()),
        "abandoned:proposal",
    }
    copied.target_proposal_keys_by_batch["abandoned-batch"] = {"abandoned:proposal"}

    # A transaction that never commits must leave the committed indexes alone.
    assert committed.command_ids_by_venue_order_id[venue_order_id] == before_venue
    assert "abandoned-venue-order" not in committed.command_ids_by_venue_order_id
    assert committed.target_proposal_keys_by_batch[batch_id] == before_batch
    assert "abandoned-batch" not in committed.target_proposal_keys_by_batch


def test_the_reducer_never_mutates_an_index_set_in_place() -> None:
    """Guards the contract the shared copy depends on.

    A set that arrives in the reducer and comes out the same object changed
    would corrupt committed state on rollback. Behavioural, not a source scan:
    the scan only forbade ``setdefault(`` and so missed a ``.get(...).discard()``
    that emptied a committed batch when a proposal key was rewritten.
    """

    def _target_event(*, sequence: int, batch_id: str) -> AccountEvent:
        return AccountEvent(
            schema_version=1,
            event_id=f"target-{sequence}",
            sequence=sequence,
            event_type=AccountEventType.TARGET.value,
            correlation_id="CORR",
            causation_id="CORR",
            account_id="demo",
            sleeve="account_execution",
            symbol="BUSDT",
            wall_ts_ns=sequence,
            monotonic_ns=sequence,
            payload={"target_key": "carry/s/c/BUSDT", "batch_id": batch_id},
            prev_event_hash="",
            state_hash="",
            event_hash="",
        )

    committed = AccountState()
    account_kernel_module.apply_account_event(committed, _target_event(sequence=1, batch_id="B-OLD"))
    before = {key: set(value) for key, value in committed.target_proposal_keys_by_batch.items()}
    assert before == {"B-OLD": {"CORR:carry/s/c/BUSDT"}}

    # Rewrite the same proposal key under a different batch on a copy. Nothing
    # this does may reach the committed index it shares.
    copied = transaction_state_copy(committed)
    account_kernel_module.apply_account_event(copied, _target_event(sequence=2, batch_id="B-NEW"))
    assert copied.target_proposal_keys_by_batch == {"B-NEW": {"CORR:carry/s/c/BUSDT"}}
    assert {
        key: set(value) for key, value in committed.target_proposal_keys_by_batch.items()
    } == before, "the reducer reached through the shared set into committed state"

    # And the index still equals a brute-force scan of what it accelerates.
    assert copied.target_proposal_keys_by_batch == _scanned_target_proposal_keys_by_batch(copied)


def test_transaction_state_copy_shares_orders_until_one_is_written(tmp_path: Path) -> None:
    committed = _indexed_journal(tmp_path).state()
    copied = transaction_state_copy(committed)
    command_id = sorted(committed.orders)[0]

    # Sharing is the point: copying every order ever held cost the owner a
    # linear slice of its whole history on every transaction.
    assert copied.orders[command_id] is committed.orders[command_id]
    assert copied.private_order_ids == set()

    written = copied.order_for_write(command_id)
    assert written is not committed.orders[command_id]
    assert asdict(written) == asdict(committed.orders[command_id])

    before = asdict(committed.orders[command_id])
    written.status = "abandoned"
    written.filled_signed_qty = written.signed_qty
    copied.put_order(
        "abandoned-command",
        OrderState(
            command_id="abandoned-command",
            batch_id="abandoned-batch",
            symbol="ABANDONEDUSDT",
            signed_qty=1.0,
            reduce_only=False,
            created_ts_ns=1,
            command_sequence=1,
        ),
    )

    # A transaction that never commits must leave the committed orders alone.
    assert asdict(committed.orders[command_id]) == before
    assert "abandoned-command" not in committed.orders
    # A second write reuses the copy it already made rather than making another.
    assert copied.order_for_write(command_id) is written


def test_transaction_state_copy_shares_positions_until_one_is_written(tmp_path: Path) -> None:
    """Positions carry the same copy-on-write contract as orders.

    They used to be copied one by one on every journaled event batch, which is
    a linear slice of the whole symbol history per transaction — and positions
    are never pruned, so a symbol traded once is copied forever after.
    """

    committed = _indexed_journal(tmp_path).state()
    symbol = sorted(committed.positions)[0]
    copied = transaction_state_copy(committed)

    assert copied.positions[symbol] is committed.positions[symbol]
    assert copied.private_position_symbols == set()

    written = copied.position_for_write(symbol)
    assert written is not committed.positions[symbol]
    assert asdict(written) == asdict(committed.positions[symbol])

    before = asdict(committed.positions[symbol])
    written.signed_qty += 7.0
    written.realized_from_fills_usdt += 3.0
    fresh = copied.position_for_write("NEVERTRADEDUSDT")
    fresh.signed_qty = 1.0

    # A transaction that never commits must leave the committed positions alone.
    assert asdict(committed.positions[symbol]) == before
    assert "NEVERTRADEDUSDT" not in committed.positions
    # A second write reuses the copy it already made rather than making another.
    assert copied.position_for_write(symbol) is written


def test_a_rolled_back_pnl_checkpoint_leaves_the_committed_position_untouched(
    tmp_path: Path,
) -> None:
    """Drive the real reducer, not the helper, and throw the transaction away.

    Positions are shared with committed state now, so this fails the moment a
    reducer write site stops going through ``position_for_write`` — which is
    the whole risk the sharing buys. Asserting through the helper instead would
    pass either way and prove nothing.
    """

    committed = _indexed_journal(tmp_path).state()
    symbol = sorted(committed.positions)[0]
    # The checkpoint must have somewhere to move, or the write is numerically a
    # no-op and the test passes whether or not the reducer privatizes.
    committed.positions[symbol].realized_from_fills_usdt = 41.0
    committed.positions[symbol].fees_from_fills_usdt = 7.0
    before = {key: asdict(value) for key, value in committed.positions.items()}

    copied = transaction_state_copy(committed)
    account_kernel_module.apply_account_event(
        copied,
        AccountEvent(
            schema_version=1,
            event_id="pnl-rollback",
            sequence=committed.events_applied + 1,
            event_type=AccountEventType.PNL.value,
            correlation_id="CORR",
            causation_id="CORR",
            account_id="demo",
            sleeve="account_execution",
            symbol=symbol,
            wall_ts_ns=1,
            monotonic_ns=1,
            payload={
                "pnl_key": "pnl-rollback",
                "source": "fill_reconstructed",
                "metadata": {"fill_accounting_checkpoint": True},
            },
            prev_event_hash="",
            state_hash="",
            event_hash="",
        ),
    )

    # The copy advanced its checkpoint; committed state must not have moved.
    assert copied.positions[symbol].reported_realized_usdt == pytest.approx(
        copied.positions[symbol].realized_from_fills_usdt
    )
    assert {
        key: asdict(value) for key, value in committed.positions.items()
    } == before, "the reducer reached through the shared position into committed state"


def test_a_rolled_back_fill_leaves_the_committed_position_untouched(tmp_path: Path) -> None:
    """The fill accounting site, driven through the reducer and discarded.

    ``_apply_fill`` is where quantity, average price, realized P&L and fees are
    written, so a shared position object leaking here corrupts the account's
    own books on any transaction that does not commit.
    """

    committed = _indexed_journal(tmp_path).state()
    symbol = sorted(committed.positions)[0]
    command_id = "rollback-command"
    # The fixture leaves every order filled, so stand up one the fill can land
    # on. This is setup, not the behaviour under test.
    acknowledged = committed.put_order(
        command_id,
        OrderState(
            command_id=command_id,
            batch_id="rollback-batch",
            symbol=symbol,
            signed_qty=1.0,
            reduce_only=False,
            created_ts_ns=1,
            command_sequence=99,
        ),
    )
    acknowledged.status = "acknowledged"
    committed.working_order_ids.add(command_id)
    before = {key: asdict(value) for key, value in committed.positions.items()}

    copied = transaction_state_copy(committed)
    account_kernel_module.apply_account_event(
        copied,
        AccountEvent(
            schema_version=1,
            event_id="fill-rollback",
            sequence=committed.events_applied + 1,
            event_type=AccountEventType.FILL.value,
            correlation_id="CORR",
            causation_id=command_id,
            account_id="demo",
            sleeve="account_execution",
            symbol=symbol,
            wall_ts_ns=1,
            monotonic_ns=1,
            payload={
                "command_id": command_id,
                "execution_id": "exec-rollback",
                "signed_qty": 0.25,
                "price": 101.0,
                "fee_usdt": 0.5,
            },
            prev_event_hash="",
            state_hash="",
            event_hash="",
        ),
    )

    assert copied.positions[symbol].fees_from_fills_usdt != before[symbol]["fees_from_fills_usdt"]
    assert {
        key: asdict(value) for key, value in committed.positions.items()
    } == before, "the fill reducer reached through the shared position into committed state"


def test_shared_orders_do_not_change_what_a_replay_reconstructs(tmp_path: Path) -> None:
    journal = _indexed_journal(tmp_path)
    state = journal.state()
    replayed = account_kernel_module.reduce_account_events(read_account_journal(tmp_path))

    # The copy-on-write sharing is bookkeeping, not journal content: the
    # rebuilt state and its hash have to match the live one exactly.
    assert {key: asdict(value) for key, value in replayed.orders.items()} == {
        key: asdict(value) for key, value in state.orders.items()
    }
    assert replayed.state_hash() == state.state_hash()


def test_free_margin_is_charged_the_batch_increase_not_the_whole_book(tmp_path: Path) -> None:
    """The venue's available margin already excludes the open book's margin.

    Charging the whole projected book against it counted the standing book
    twice and refused entries the venue would have funded, capping the account
    at roughly half its equity — well inside the declared envelope.
    """

    kernel = _kernel(tmp_path)
    policy = AccountRiskPolicy(
        max_component_gross_notional_usdt=10_000.0,
        max_account_gross_notional_usdt=10_000.0,
        max_symbol_notional_usdt=10_000.0,
        max_initial_margin_usdt=2_500.0,
        max_leverage=2.0,
    )

    def submit(batch: str, qty: float, free_margin: float):
        return kernel.submit_targets(
            batch_id=batch,
            market_inputs=[_market()],
            targets=[_target(decision=batch, key="carry/main/BUSDT", sleeve="carry", qty=qty, leverage=2.0)],
            risk_snapshot=AccountRiskSnapshot(
                equity_usdt=2_500.0,
                available_margin_usdt=free_margin,
                snapshot_key=f"wallet-{batch}",
                snapshot_ts_ns=950_000_000,
            ),
            risk_policy=policy,
            instrument_rules=_rules(),
        )

    # Book grows to 120 qty @ 10 = 1200 notional, 600 margin at 2x.
    first = submit("b1", 120.0, 2_500.0)
    assert first.accepted, first.rejection_keys

    # The venue now holds 600 of margin, so it reports 1900 free. Growing the
    # book to 2400 notional needs another 600 — the venue has 1900 spare and
    # the declared margin ceiling is 2500, so this has to be admitted.
    second = submit("b2", 240.0, 1_900.0)
    assert second.accepted, second.rejection_keys

    # Growing past the declared ceiling is still refused: 2x on 6000 notional
    # is 3000 of margin against a 2500 cap.
    beyond = submit("b3", 600.0, 1_300.0)
    assert not beyond.accepted
    assert any("initial_margin_limit" in key for key in beyond.rejection_keys)


def test_free_margin_still_refuses_a_batch_the_venue_cannot_fund(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    policy = AccountRiskPolicy(
        max_component_gross_notional_usdt=10_000.0,
        max_account_gross_notional_usdt=10_000.0,
        max_symbol_notional_usdt=10_000.0,
        max_initial_margin_usdt=10_000.0,
        max_leverage=2.0,
    )
    result = kernel.submit_targets(
        batch_id="broke",
        market_inputs=[_market()],
        targets=[_target(decision="d", key="carry/main/BUSDT", sleeve="carry", qty=200.0, leverage=2.0)],
        risk_snapshot=AccountRiskSnapshot(
            equity_usdt=2_500.0,
            available_margin_usdt=10.0,
            snapshot_key="wallet-broke",
            snapshot_ts_ns=950_000_000,
        ),
        risk_policy=policy,
        instrument_rules=_rules(),
    )
    assert not result.accepted
    assert any("available_margin_limit" in key for key in result.rejection_keys)


def _priced_market(*, price: float = 10.0) -> MarketInputRef:
    """A book observed at a realistic wall clock, so producer-price ages are expressible."""

    return MarketInputRef(
        input_key="book-priced",
        symbol="BUSDT",
        exchange_ts_ns=1_800_000_000_000_000_000,
        local_receive_ts_ns=1_800_000_000_000_000_000,
        reference_price=price,
        bid_price=price - 0.01,
        ask_price=price + 0.01,
        book_sequence=42,
        source="test_l2",
    )


def test_a_fresh_producer_price_sizes_the_order_instead_of_the_live_one() -> None:
    market = _priced_market()
    intent = SleeveTargetIntent(
        decision_key="producer-priced",
        target_key="long/main/BUSDT",
        strategy_id="long-v1",
        component_id="main",
        symbol="BUSDT",
        signed_notional_usdt=20.0,
        leverage=10.0,
        reason="producer price",
        metadata={
            "decision_reference_price": 8.0,
            # 500 ms before the book observation.
            "decision_reference_price_ts_ms": 1_800_000_000_000 - 500,
        },
    )

    target = LongTargetAdapter(producer_price_max_age_ns=60_000_000_000).desired_target(
        intent, market, _rules()["BUSDT"]
    )

    assert target.reference_price == 8.0
    assert target.metadata["raw_signed_qty"] == pytest.approx(20.0 / 8.0)
    assert target.metadata["sizing_price_source"] == "producer_decision"


def test_a_producer_price_past_its_age_bound_is_refused_for_the_live_one() -> None:
    market = _priced_market()
    intent = SleeveTargetIntent(
        decision_key="stale-producer-price",
        target_key="long/main/BUSDT",
        strategy_id="long-v1",
        component_id="main",
        symbol="BUSDT",
        signed_notional_usdt=20.0,
        leverage=10.0,
        reason="stale producer price",
        metadata={
            "decision_reference_price": 8.0,
            # 61s before the book observation, past the 60s bound.
            "decision_reference_price_ts_ms": 1_800_000_000_000 - 61_000,
        },
    )

    target = LongTargetAdapter(producer_price_max_age_ns=60_000_000_000).desired_target(
        intent, market, _rules()["BUSDT"]
    )

    assert target.reference_price == 10.0
    assert target.metadata["sizing_price_source"] == "live_market"


def test_producer_pricing_is_off_until_it_is_configured_on() -> None:
    market = _priced_market()
    intent = SleeveTargetIntent(
        decision_key="default-off",
        target_key="long/main/BUSDT",
        strategy_id="long-v1",
        component_id="main",
        symbol="BUSDT",
        signed_notional_usdt=20.0,
        leverage=10.0,
        reason="default",
        metadata={
            "decision_reference_price": 8.0,
            "decision_reference_price_ts_ms": 1_800_000_000_000 - 500,
        },
    )

    target = LongTargetAdapter().desired_target(intent, market, _rules()["BUSDT"])

    assert target.reference_price == 10.0
    assert target.metadata["sizing_price_source"] == "live_market"


def test_an_exit_always_sizes_off_the_live_price() -> None:
    """A reduction priced from a stale number leaves a residue behind."""

    market = _priced_market()
    intent = SleeveTargetIntent(
        decision_key="risk-exit",
        target_key="risk/main/BUSDT",
        strategy_id="risk",
        component_id="main",
        symbol="BUSDT",
        signed_notional_usdt=0.0,
        leverage=10.0,
        reason="flatten",
        metadata={
            "owner_sleeve": "long",
            "decision_reference_price": 8.0,
            "decision_reference_price_ts_ms": 1_800_000_000_000 - 500,
        },
    )

    set_producer_price_max_age_ns(60_000_000_000)
    try:
        target = RiskTargetAdapter().desired_target(intent, market, _rules()["BUSDT"])
    finally:
        set_producer_price_max_age_ns(0)

    assert target.reference_price == 10.0
    assert target.metadata["sizing_price_source"] == "live_market"


def test_a_projection_touched_behind_this_process_is_still_repaired(tmp_path: Path) -> None:
    """The cached tail must never let a changed file go unnoticed.

    The continuity check reads the end of ``events.jsonl`` on every commit to
    learn a hash this process just wrote. It is now skipped while a ``stat``
    agrees on the size, which is what makes it cheap -- so the thing worth
    testing is the case the cache must not swallow: someone else changed the
    file underneath it. Any size the cache did not produce sends it back to the
    read, and a mismatch there rebuilds the projection from the segments.
    """

    kernel = _kernel(tmp_path)

    def commit(decision: str, qty: float) -> None:
        kernel.submit_targets(
            batch_id=f"batch-{decision}",
            market_inputs=[_market()],
            targets=[_target(decision=decision, key="continuous/main/BUSDT", sleeve="continuous", qty=qty)],
            risk_snapshot=_snapshot(),
            risk_policy=_policy(),
            instrument_rules=_rules(),
        )

    commit("d1", -2.0)
    path = account_journal_path(tmp_path)
    healthy = path.read_bytes()
    commit("d2", -3.0)
    # Warm run: the projection keeps growing and stays a faithful rendering of
    # the authoritative segments.
    assert path.read_bytes().splitlines() == [
        canonical_json(event.to_dict()) for event in read_account_journal(tmp_path, verify=True)
    ]

    # Now truncate it behind this process, exactly as an interrupted write would.
    path.write_bytes(healthy)
    commit("d3", -4.0)

    rebuilt = path.read_bytes().splitlines()
    authoritative = read_account_journal(tmp_path, verify=True)
    assert rebuilt == [canonical_json(event.to_dict()) for event in authoritative]
    assert len(authoritative) > 3


def test_the_snapshot_view_is_reused_until_the_journal_moves(tmp_path: Path) -> None:
    """Every protection check and the order path ask for one of these.

    It used to copy the whole account into a fresh tuple each time -- 16,059
    events on the demo book after a day of trading, and history is never
    pruned, so the cost grew with the account's age. What must hold is that a
    commit still produces a new view: a stale one would let a reader act on a
    journal head that has moved.
    """

    kernel = _kernel(tmp_path)

    def commit(decision: str, qty: float) -> None:
        kernel.submit_targets(
            batch_id=f"snapshot-{decision}",
            market_inputs=[_market()],
            targets=[_target(decision=decision, key="continuous/main/BUSDT", sleeve="continuous", qty=qty)],
            risk_snapshot=_snapshot(),
            risk_policy=_policy(),
            instrument_rules=_rules(),
        )

    commit("d1", -2.0)
    first_events, first_state = kernel.journal._snapshot_ref()
    again_events, again_state = kernel.journal._snapshot_ref()

    assert again_events is first_events, "an unchanged journal must not be recopied"
    assert again_state is first_state

    commit("d2", -3.0)
    moved_events, moved_state = kernel.journal._snapshot_ref()

    assert moved_events is not first_events, "a commit must produce a new view"
    assert len(moved_events) > len(first_events)
    assert moved_events[: len(first_events)] == first_events
    # The view still describes one coherent head, which is what its readers check.
    assert moved_state.events_applied == len(moved_events)
    assert moved_state.rolling_state_hash == moved_events[-1].state_hash
