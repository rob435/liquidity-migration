from __future__ import annotations

from pathlib import Path

from liquidity_migration.account_kernel import (
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
)
from liquidity_migration.account_paper_runner import FixedCapitalSnapshotProvider
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.account_service import (
    AccountExecutionService,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from liquidity_migration.account_service_bybit import (
    CapturedBybitMarketProvider,
    CapturedPaperExecutionAdapter,
    VerifiedBybitDemoRulesProvider,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.execution_adapters import BookLevel, ExecutionTwinConfig, L2BookSnapshot, LatencyProfile
from liquidity_migration.execution_adapters import MarketOrderExecutionTwin
from liquidity_migration.historical_account_replay import (
    HistoricalAccountSession,
    HistoricalAccountReplay,
    HistoricalReplayCycle,
    HistoricalTargetDecision,
    historical_cycles_from_decisions,
    synthetic_historical_rules,
    synthetic_historical_rules_for_symbols,
)
from liquidity_migration.kernel_parity import compare_kernel_journals
from liquidity_migration.market_capture import MarketCaptureConfig, SequenceAwareMarketRecorder
from liquidity_migration.strategy_runtime import SleeveTargetIntent


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


def _cycles() -> list[HistoricalReplayCycle]:
    return [
        HistoricalReplayCycle(
            batch_id="entry",
            wall_ts_ns=1_000,
            books={"BUSDT": _book(sequence=1, local_ns=1_000, bid=9.9, ask=10.1)},
            intents=(_intent(decision="entry-d", notional=20.0),),
            risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet-1", 990),
        ),
        HistoricalReplayCycle(
            batch_id="exit",
            wall_ts_ns=2_000,
            books={"BUSDT": _book(sequence=2, local_ns=2_000, bid=11.0, ask=11.2)},
            intents=(_intent(decision="exit-d", notional=0.0),),
            risk_snapshot=AccountRiskSnapshot(102.0, 102.0, "wallet-2", 1_990),
        ),
    ]


def _run(root: Path):
    return HistoricalAccountReplay(
        root,
        account_id="replay-account",
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 100.0, 10.0),
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        execution_config=ExecutionTwinConfig(
            fee_bps=5.5,
            latency=LatencyProfile(1, 1, 1),
            max_decision_age_ns=100,
        ),
        id_seed="replay-test",
    ).run(_cycles())


def test_historical_replay_consumes_timestamped_targets_not_finished_trades(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert [batch.batch_id for batch in result.batches] == ["entry", "exit"]
    assert all(batch.accepted for batch in result.batches)
    assert result.batches[0].commands[0].side == "Buy"
    assert result.batches[1].commands[0].side == "Sell"
    assert result.batches[1].commands[0].reduce_only


def test_online_historical_session_returns_risk_feedback_before_next_decision(
    tmp_path: Path,
) -> None:
    session = HistoricalAccountSession(
        tmp_path,
        account_id="online-account",
        risk_policy=AccountRiskPolicy(10.0, 10.0, 10.0, 10.0, 10.0),
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
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
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
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
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


def test_same_target_tape_has_identical_journal_across_named_environment_roots(tmp_path: Path) -> None:
    roots = {name: tmp_path / name for name in ("historical", "paper", "demo")}
    hashes = [_run(root).final_state_hash for root in roots.values()]
    assert hashes[0] == hashes[1] == hashes[2]
    assert compare_kernel_journals(
        roots,
        comparison_batch_ids=["entry", "exit"],
        quantity_tolerance=1e-12,
    ).passed


def test_real_paper_service_and_historical_replay_share_kernel_parity(tmp_path: Path) -> None:
    wall_ns = 1_800_000_000_010_000_000
    clock = VirtualClock(current_wall_ns=wall_ns, current_monotonic_ns=0)
    recorder = SequenceAwareMarketRecorder(
        tmp_path / "capture",
        config=MarketCaptureConfig(
            depth=50,
            segment_max_bytes=1_000_000,
            fsync_every_records=1,
            min_free_disk_bytes=1,
            ring_records_per_symbol=100,
        ),
        clock=clock,
    )
    recorder.on_message({
        "topic": "orderbook.50.BUSDT",
        "type": "snapshot",
        "ts": 1_800_000_000_000,
        "cts": 1_799_999_999_999,
        "data": {"s": "BUSDT", "b": [["9.9", "100"]], "a": [["10.1", "100"]], "u": 1, "seq": 1},
    }, local_receive_ts_ns=wall_ns)
    book = recorder.current_book("BUSDT")
    assert book is not None
    rules = {
        "BUSDT": InstrumentRules(
            "BUSDT", 0.1, 0.1, 1.0, environment="demo", source="test"
        )
    }
    policy = AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 100.0, 10.0)
    execution_config = ExecutionTwinConfig(
        fee_bps=5.5,
        latency=LatencyProfile(1, 1, 1),
        max_decision_age_ns=100,
    )
    requested = _intent(decision="entry-d", notional=20.0)
    paper_root = tmp_path / "paper"
    route = ensure_account_route(
        account_id="parity-account",
        environment="paper",
        account_root=paper_root,
        inbox_root=tmp_path / "paper-inbox",
    )
    request = AccountTargetRequest(
        request_id="entry-request",
        batch_id="entry",
        created_ts_ns=wall_ns,
        route_id=route.route_id,
        account_id=route.account_id,
        environment=route.environment,
        intents=(requested,),
    )
    paper_kernel = AccountExecutionKernel(
        route.account_path,
        account_id=route.account_id,
        clock=clock,
        id_seed="parity-seed",
    )
    market_provider = CapturedBybitMarketProvider(recorder)
    twin = MarketOrderExecutionTwin(
        books={},
        instrument_rules=rules,
        config=execution_config,
        name="paper",
        id_seed="parity-seed:execution",
    )
    paper_service = AccountExecutionService(
        route=route,
        kernel=paper_kernel,
        market_provider=market_provider,
        snapshot_provider=FixedCapitalSnapshotProvider(100.0, clock=clock),
        rules_provider=VerifiedBybitDemoRulesProvider(rules),
        risk_policy=policy,
        execution_adapter=CapturedPaperExecutionAdapter(
            market_provider=market_provider,
            twin=twin,
        ),
        clock=clock,
        required_rules_environment="demo",
    )
    paper_service.handle(request)

    historical_root = tmp_path / "historical-real"
    HistoricalAccountReplay(
        historical_root,
        account_id="parity-account",
        risk_policy=policy,
        instrument_rules=rules,
        execution_config=execution_config,
        id_seed="parity-seed",
    ).run([HistoricalReplayCycle(
        batch_id="entry",
        wall_ts_ns=wall_ns,
        books={"BUSDT": book},
        intents=(requested,),
        risk_snapshot=AccountRiskSnapshot(
            100.0,
            100.0,
            "paper-fixed:100:entry",
            wall_ns,
        ),
    )])

    report = compare_kernel_journals(
        {"historical": historical_root, "paper": paper_root, "demo": paper_root},
        comparison_batch_ids=["entry"],
        quantity_tolerance=1e-12,
    )
    assert report.passed, report.mismatches
    recorder.close()


def test_same_timestamp_retarget_is_exit_first_without_splitting_other_components() -> None:
    exit_intent = _intent(decision="exit-old", notional=0.0)
    replacement = _intent(decision="enter-new", notional=30.0)
    other = RequestedIntent(
        adapter_kind=SleeveAdapterKind.CONTINUOUS,
        intent=SleeveTargetIntent(
            decision_key="other-entry",
            target_key="continuous/other/BUSDT",
            strategy_id="continuous-v1",
            component_id="other",
            symbol="BUSDT",
            signed_notional_usdt=-10.0,
            leverage=4.0,
            reason="other-entry",
        ),
    )
    cycles = historical_cycles_from_decisions(
        [
            HistoricalTargetDecision(2_000, replacement, 10.0),
            HistoricalTargetDecision(2_000, other, 10.0),
            HistoricalTargetDecision(2_000, exit_intent, 10.0),
        ],
        equity_usdt=100.0,
    )
    assert len(cycles) == 2
    assert [item.intent.decision_key for item in cycles[0].intents] == [
        "exit-old",
        "other-entry",
    ]
    assert [item.intent.decision_key for item in cycles[1].intents] == ["enter-new"]


def test_same_symbol_atomic_batch_rejects_conflicting_reference_prices() -> None:
    other = RequestedIntent(
        adapter_kind=SleeveAdapterKind.CONTINUOUS,
        intent=SleeveTargetIntent(
            decision_key="other-entry",
            target_key="continuous/other/BUSDT",
            strategy_id="continuous-v1",
            component_id="other",
            symbol="BUSDT",
            signed_notional_usdt=-10.0,
            leverage=4.0,
            reason="other-entry",
        ),
    )
    try:
        historical_cycles_from_decisions(
            [
                HistoricalTargetDecision(2_000, _intent(decision="entry", notional=20.0), 10.0),
                HistoricalTargetDecision(2_000, other, 10.1),
            ],
            equity_usdt=100.0,
        )
    except ValueError as exc:
        assert "share a reference price" in str(exc)
    else:
        raise AssertionError("conflicting same-symbol prices were silently accepted")


def test_synthetic_rules_keep_maximum_leverage_across_symbol_decisions() -> None:
    rules = synthetic_historical_rules([
        HistoricalTargetDecision(2_000, _intent(decision="entry", notional=20.0), 10.0),
        HistoricalTargetDecision(3_000, _intent(decision="exit", notional=0.0), 11.0),
    ])
    assert rules["BUSDT"].max_leverage == 10.0
    assert rules["BUSDT"].observed_ts_ns == 3_000
