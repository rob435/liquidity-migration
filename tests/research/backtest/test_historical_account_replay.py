from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.account.account_kernel import (
    AccountEventType,
    AccountJournalIntegrityError,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
    read_account_journal,
)
from liquidity_migration.account.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account.account_service import (
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from liquidity_migration.account.execution_adapters import (
    BookLevel,
    ExecutionObservation,
    ExecutionObservationType,
    ExecutionTwinConfig,
    L2BookSnapshot,
    LatencyProfile,
)
from liquidity_migration.research.backtest.historical_account_replay import (
    HistoricalAccountSession,
    HistoricalReplayCycle,
    HistoricalTargetDecision,
    synthetic_historical_rules_for_symbols,
)
from liquidity_migration.account.strategy_runtime import SleeveTargetIntent


def _book(*, sequence: int, local_ns: int, bid: float, ask: float) -> L2BookSnapshot:
    return L2BookSnapshot(
        symbol="BUSDT",
        sequence=sequence,
        previous_sequence=sequence - 1,
        exchange_ts_ns=local_ns - 10,
        local_receive_ts_ns=local_ns,
        bids=(BookLevel(bid, 100.0),),
        asks=(BookLevel(ask, 100.0),),
    )


def _intent(*, decision: str, notional: float) -> RequestedIntent:
    return RequestedIntent(
        adapter_kind=SleeveAdapterKind.LONG,
        intent=SleeveTargetIntent(
            decision_key=decision,
            target_key="long/main/BUSDT",
            strategy_id="long-v1",
            component_id="main",
            symbol="BUSDT",
            signed_notional_usdt=notional,
            leverage=10.0,
            reason=decision,
        ),
    )


def _owner_request(
    route: AccountRoute,
    *,
    request_id: str,
    created_ts_ns: int,
    adapter_kind: SleeveAdapterKind,
    target_key: str,
    notional: float,
) -> AccountTargetRequest:
    return AccountTargetRequest(
        request_id=request_id,
        batch_id=request_id,
        created_ts_ns=created_ts_ns,
        route_id=route.route_id,
        account_id=route.account_id,
        environment=route.environment,
        intents=(RequestedIntent(
            adapter_kind=adapter_kind,
            intent=SleeveTargetIntent(
                decision_key=f"decision:{request_id}",
                target_key=target_key,
                strategy_id="historical-owner-test",
                component_id=target_key.split("/")[-2],
                symbol="BUSDT",
                signed_notional_usdt=notional,
                leverage=10.0,
                reason="test",
                metadata={"decision_reference_price": 10.0},
            ),
        ),),
    )


class _RejectExecution:
    name = "historical-owner-reject"

    def __init__(self) -> None:
        self.submissions = 0

    def submit(self, command, market_input):
        del market_input
        self.submissions += 1
        return (ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=command.created_ts_ns,
            local_receive_ts_ns=command.created_ts_ns,
            accepted=False,
            rejection_key="test:historical-convergence-rejected",
        ),)


def test_online_historical_session_returns_risk_feedback_before_next_decision(
    tmp_path: Path,
) -> None:
    session = HistoricalAccountSession(
        tmp_path,
        account_id="online-account",
        risk_policy=AccountRiskPolicy(10.0, 10.0, 10.0, 10.0),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="online-feedback",
    )
    cycle = HistoricalReplayCycle(
        batch_id="too-large",
        wall_ts_ns=1_000,
        books={"BUSDT": _book(sequence=1, local_ns=1_000, bid=9.9, ask=10.1)},
        intents=(_intent(decision="too-large", notional=20.0),),
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet", 990),
    )
    output = session.process_cycle(cycle)
    assert output.target_result.accepted is False
    assert any("component_gross_limit" in key for key in output.target_result.rejection_keys)
    assert output.execution_events == ()
    assert session.kernel is not None
    assert session.kernel.state().positions == {}


def test_online_historical_session_preserves_execution_rate_limit_state(
    tmp_path: Path,
) -> None:
    session = HistoricalAccountSession(
        tmp_path,
        account_id="online-account",
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
            rate_limit_orders=1,
            rate_limit_window_ns=1_000,
        ),
        id_seed="online-rate-limit",
    )
    first = session.process_cycle(HistoricalReplayCycle(
        batch_id="entry-1",
        wall_ts_ns=1_000,
        books={"BUSDT": _book(sequence=1, local_ns=1_000, bid=10.0, ask=10.0)},
        intents=(_intent(decision="entry-1", notional=20.0),),
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet-1", 1_000),
    ))
    second = session.process_cycle(HistoricalReplayCycle(
        batch_id="entry-2",
        wall_ts_ns=1_500,
        books={"BUSDT": _book(sequence=2, local_ns=1_500, bid=10.0, ask=10.0)},
        intents=(_intent(decision="entry-2", notional=30.0),),
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet-2", 1_500),
    ))
    assert first.target_result.accepted and second.target_result.accepted
    rejected_acks = [
        event for event in second.execution_events
        if event.event_type == AccountEventType.ACK.value
        and event.payload.get("accepted") is False
    ]
    assert len(rejected_acks) == 1
    assert rejected_acks[0].payload["metadata"]["reason"] == "rate_limit"
    assert session.kernel is not None
    assert session.kernel.state().positions["BUSDT"].signed_qty == 2.0


def test_online_submit_decisions_builds_causal_cycle_and_feedback(tmp_path: Path) -> None:
    rules = synthetic_historical_rules_for_symbols(
        ["busdt"], max_leverage=10.0, observed_ts_ns=1_000
    )
    session = HistoricalAccountSession(
        tmp_path,
        account_id="online-decisions",
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules=rules,
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="online-decisions",
    )
    decision = HistoricalTargetDecision(
        wall_ts_ns=2_000,
        reference_price=10.0,
        intent=_intent(decision="online-entry", notional=20.0),
    )
    outputs = session.submit_decisions([decision], equity_usdt=100.0)
    assert len(outputs) == 1
    assert outputs[0].target_result.accepted
    assert outputs[0].target_result.commands[0].side == "Buy"
    assert session.kernel is not None
    assert session.kernel.state().positions["BUSDT"].signed_qty == 2.0


def test_online_submit_request_preserves_published_batch_and_content_identity(
    tmp_path: Path,
) -> None:
    rules = synthetic_historical_rules_for_symbols(
        ["BUSDT"], max_leverage=10.0, observed_ts_ns=1_000
    )


    session = HistoricalAccountSession(
        tmp_path,
        account_id="published-account",
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules=rules,
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="published-account",
    )
    request = AccountTargetRequest(
        request_id="target-request-one",
        batch_id="published-batch-one",
        created_ts_ns=2_000,
        route_id="historical-route",
        account_id="published-account",
        environment="demo",
        intents=(_intent(decision="published-entry", notional=20.0),),
    )

    outputs = session.submit_request(
        request,
        equity_usdt=100.0,
        market_prices={"BUSDT": 10.0},
        market_observed_ts_ns=1_900,
    )

    assert len(outputs) == 1
    assert outputs[0].target_result.accepted
    assert session.kernel is not None
    target = session.kernel._state_ref().component_targets["long/main/BUSDT"]
    assert target["metadata"]["account_request_id"] == request.request_id
    assert (
        target["metadata"]["account_request_created_ts_ns"]
        == request.created_ts_ns
    )
    events = read_account_journal(tmp_path, verify=True)
    risk = [event for event in events if event.event_type == AccountEventType.RISK_DECISION.value]
    assert len(risk) == 1
    assert risk[0].payload["batch_id"] == request.batch_id
    market = [
        event
        for event in events
        if event.event_type == AccountEventType.MARKET_INPUT_REF.value
    ]
    assert len(market) == 1
    assert market[0].wall_ts_ns == request.created_ts_ns
    assert market[0].payload["exchange_ts_ns"] == 1_900
    assert market[0].payload["local_receive_ts_ns"] == 1_900

    conflicting_request = AccountTargetRequest(
        request_id="target-request-two",
        batch_id=request.batch_id,
        created_ts_ns=request.created_ts_ns,
        route_id=request.route_id,
        account_id=request.account_id,
        environment=request.environment,
        intents=request.intents,
    )
    with pytest.raises(AccountJournalIntegrityError, match="request content changed"):
        session.submit_request(
            conflicting_request,
            equity_usdt=100.0,
            market_prices={"BUSDT": 10.0},
        )


def test_owner_convergence_fails_immediately_on_execution_rejection(
    tmp_path: Path,
) -> None:
    route = ensure_account_route(
        account_id="historical-owner-convergence",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )
    session = HistoricalAccountSession(
        route.account_path,
        account_id=route.account_id,
        risk_policy=AccountRiskPolicy(
            1_000.0,
            1_000.0,
            1_000.0,
            10.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT",
                qty_step=0.1,
                min_qty=0.1,
                min_notional=0.0,
                max_order_qty=100.0,
                max_leverage=10.0,
            )
        },
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="historical-owner-convergence",
        route=route,
    )
    for request in (
        _owner_request(
            route,
            request_id="open-short",
            created_ts_ns=1_000,
            adapter_kind=SleeveAdapterKind.CONTINUOUS,
            target_key="continuous/main/BUSDT",
            notional=-50.0,
        ),
        _owner_request(
            route,
            request_id="open-long",
            created_ts_ns=1_100,
            adapter_kind=SleeveAdapterKind.LONG,
            target_key="long/main/BUSDT",
            notional=20.0,
        ),
        _owner_request(
            route,
            request_id="close-short",
            created_ts_ns=1_200,
            adapter_kind=SleeveAdapterKind.CONTINUOUS,
            target_key="continuous/main/BUSDT",
            notional=0.0,
        ),
    ):
        submitted = session.submit_request_via_owner(
            request,
            equity_usdt=100.0,
            market_prices={"BUSDT": 10.0},
            market_observed_ts_ns=request.created_ts_ns,
        )
        assert submitted.feedback.accepted

    assert session.kernel is not None
    staged = session.kernel.state()
    assert staged.positions["BUSDT"].signed_qty == pytest.approx(0.0)
    assert staged.aggregate_targets["BUSDT"] == pytest.approx(2.0)
    assert session.account_service is not None
    session.account_service.risk_policy = AccountRiskPolicy(
        1.0,
        1.0,
        0.1,
        1.0,
    )
    blocked = session.converge_until_stable()
    assert len(blocked) == 1 and not blocked[0].accepted
    assert any("component_gross_limit" in key for key in blocked[0].rejection_keys)
    assert session.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(0.0)

    session.account_service.risk_policy = session.risk_policy
    rejection = _RejectExecution()
    session.account_service.execution_adapter = rejection

    with pytest.raises(
        RuntimeError,
        match="historical account convergence execution rejected",
    ):
        session.converge_until_stable()

    assert rejection.submissions == 1
    rejected_state = session.kernel.state()
    assert rejected_state.positions["BUSDT"].signed_qty == pytest.approx(0.0)
    assert rejected_state.aggregate_targets["BUSDT"] == pytest.approx(2.0)
