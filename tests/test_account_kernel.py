from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path

import pytest

import liquidity_migration.account_kernel as account_kernel_module
from liquidity_migration.account_kernel import (
    AccountEvent,
    AccountEventType,
    AccountExecutionKernel,
    AccountJournalIntegrityError,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    OrderCommand,
    account_journal_path,
    account_transactions_path,
    read_account_journal,
    read_account_journal_head,
    verify_account_journal,
)
from liquidity_migration.bybit_errors import BybitRequestRejected, BybitSubmissionUncertain
from liquidity_migration.bybit_execution_adapter import BybitDemoExecutionAdapter
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.execution_adapters import (
    BookLevel,
    ExecutionObservation,
    ExecutionTwinConfig,
    KernelExecutionDriver,
    L2BookSnapshot,
    LatencyProfile,
    MarketOrderExecutionTwin,
)
from liquidity_migration.strategy_runtime import (
    AccountKernelRuntime,
    AdaptedIntent,
    ContinuousTargetAdapter,
    HedgeTargetAdapter,
    LongTargetAdapter,
    RiskTargetAdapter,
    SleeveTargetIntent,
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
    )


def _rules(
    *,
    min_notional: float = 1.0,
    max_order_qty: float = 100.0,
) -> dict[str, InstrumentRules]:
    return {
        "BUSDT": InstrumentRules(
            symbol="BUSDT",
            qty_step=0.1,
            min_qty=0.1,
            min_notional=min_notional,
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
    assert list(kernel._state_ref().venue_snapshots) == [
        "second-shared-history-append"
    ]


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
        event
        for event in kernel.journal.events()
        if event.event_type == AccountEventType.VENUE_SNAPSHOT.value
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

    workers = [
        threading.Thread(target=finalize, args=(1_600_000_000 + offset,))
        for offset in (0, 1)
    ]
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


def test_bybit_demo_adapter_refuses_mainnet_and_never_synthesizes_a_fill() -> None:
    class FakeClient:
        demo = False

    with pytest.raises(ValueError, match="mainnet is forbidden"):
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

        def place_order(self, **_params: object) -> dict[str, str]:
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
