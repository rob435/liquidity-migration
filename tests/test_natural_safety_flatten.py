from __future__ import annotations

import hashlib
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from liquidity_migration.account_intent_client import (
    AccountTargetPublisher,
    ExitFirstPublication,
    PublishedTargetRequest,
    component_target_key,
    requested_target,
)
from liquidity_migration.account_kernel import (
    GENESIS_HASH,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.account_owner_health import (
    TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    TEST_ACCOUNT_OWNER_INVOCATION_ID,
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    write_account_owner_health,
)
from liquidity_migration.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account_service import (
    AccountIntentInbox,
    AccountServiceReceipt,
    RequestedIntent,
    SleeveAdapterKind,
)
from liquidity_migration.captured_account_replay import (
    POST_WINDOW_SAFETY_SCOPE,
    build_post_window_safety_manifest,
    load_post_window_safety_manifest,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.natural_safety_flatten import (
    SAFETY_REASON,
    SAFETY_STRATEGY_PROFILE,
    active_component_desires,
    publish_natural_safety_flatten,
)
from liquidity_migration.strategy_event_clock import StrategyEvent
from liquidity_migration.strategy_target_replay import (
    JsonlTargetSchedulingCaptureTape,
    PublishedTargetCyclePayload,
    load_target_scheduling_capture,
)


ACCOUNT_ID = "natural-safety-demo"
T1_NS = 2_000_000_000
NOW_NS = 3_000_000_000


def _route(tmp_path: Path) -> AccountRoute:
    return ensure_account_route(
        account_id=ACCOUNT_ID,
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _market(symbol: str, *, price: float = 100.0) -> MarketInputRef:
    return MarketInputRef(
        input_key=f"market:{symbol}",
        symbol=symbol,
        exchange_ts_ns=1_000_000_000,
        local_receive_ts_ns=1_000_000_001,
        reference_price=price,
        bid_price=price - 0.1,
        ask_price=price + 0.1,
        book_sequence=1,
        source="test",
    )


def _rules(symbol: str) -> InstrumentRules:
    return InstrumentRules(
        symbol=symbol,
        qty_step=0.1,
        min_qty=0.1,
        min_notional=1.0,
        tick_size=0.1,
        max_order_qty=100.0,
        max_leverage=10.0,
        source="test",
        environment="demo",
        observed_ts_ns=1_000_000_000,
    )


def _policy() -> AccountRiskPolicy:
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=2_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=1_000.0,
        max_leverage=10.0,
    )


def _snapshot() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        equity_usdt=10_000.0,
        available_margin_usdt=9_000.0,
        snapshot_key="wallet",
        snapshot_ts_ns=1_000_000_000,
    )


def _kernel(route: AccountRoute) -> AccountExecutionKernel:
    return AccountExecutionKernel(
        route.account_path,
        account_id=route.account_id,
        clock=VirtualClock(
            current_wall_ns=1_100_000_000,
            current_monotonic_ns=100_000_000,
        ),
        id_seed="natural-safety-test",
    )


def _open_component(
    kernel: AccountExecutionKernel,
    *,
    sleeve: SleeveAdapterKind,
    strategy_id: str,
    component_id: str,
    symbol: str,
    signed_qty: float,
    target_key: str | None = None,
) -> str:
    key = target_key or component_target_key(
        sleeve=sleeve,
        strategy_id=strategy_id,
        component_id=component_id,
        symbol=symbol,
    )
    current_symbols = {
        str(row["symbol"])
        for row in kernel.state().component_targets.values()
    }
    all_symbols = sorted(current_symbols | {symbol})
    result = kernel.submit_targets(
        batch_id=f"open:{strategy_id}:{component_id}",
        market_inputs=[_market(current_symbol) for current_symbol in all_symbols],
        targets=[
            DesiredTarget(
                decision_key=f"decision:{strategy_id}:{component_id}",
                target_key=key,
                sleeve=sleeve.value,
                strategy_id=strategy_id,
                component_id=component_id,
                symbol=symbol,
                signed_qty=signed_qty,
                reference_price=100.0,
                leverage=2.0,
                reason="test open",
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules={
            current_symbol: _rules(current_symbol) for current_symbol in all_symbols
        },
    )
    assert result.accepted and len(result.commands) == 1
    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id=f"venue:{component_id}",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_200_000_001,
    )
    kernel.record_fill(
        command_id=command.command_id,
        execution_id=f"fill:{component_id}",
        signed_qty=command.signed_qty,
        price=100.0,
        fee_usdt=0.01,
        exchange_ts_ns=1_200_000_002,
        local_receive_ts_ns=1_200_000_003,
    )
    return key


def _publish_health(kernel: AccountExecutionKernel, route: AccountRoute) -> None:
    state = kernel.state()
    write_account_owner_health(
        route.account_path,
        AccountOwnerHealth(
            owner="account_execution",
            environment="demo",
            account_id=route.account_id,
            status=AccountOwnerHealthStatus.HEALTHY,
            observed_ts_ns=NOW_NS,
            loop_sequence=max(state.events_applied, 1),
            journal_sequence=state.events_applied,
            journal_state_hash=state.rolling_state_hash,
            equity_usdt=10_000.0,
            available_margin_usdt=9_000.0,
            requested_symbols_ready=True,
            invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
        ),
    )


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "safety-targets.jsonl", tmp_path / "safety-manifest.json"


def test_safety_flatten_rejects_weakened_owner_health_bound(tmp_path: Path) -> None:
    route = _route(tmp_path)
    capture, manifest = _paths(tmp_path)
    with pytest.raises(ValueError, match="registered 30 seconds"):
        publish_natural_safety_flatten(
            route=route,
            freeze_id="natural-v1",
            t1_ns=T1_NS,
            target_capture_path=capture,
            manifest_output_path=manifest,
            max_owner_health_age_ns=TARGET_PRODUCER_HEALTH_MAX_AGE_NS + 1,
            now_ns=NOW_NS,
        )


def _write_direct_safety_capture(
    path: Path,
    *,
    route: AccountRoute,
    publication: ExitFirstPublication,
    profile: str = SAFETY_STRATEGY_PROFILE,
    freeze_id: str = "natural-v1",
    event_ts_ns: int = T1_NS + 1,
) -> None:
    JsonlTargetSchedulingCaptureTape(path).append_from_cycle(
        StrategyEvent(
            event_ts_ns=event_ts_ns,
            ingest_ts_ns=event_ts_ns,
            source="long:demo",
            source_sequence=0,
            kind="timer",
            payload={
                "execution_environment": "demo",
                "strategy_profile": profile,
                "natural_safety_flatten": True,
                "natural_freeze_id": freeze_id,
                "natural_t1_ns": T1_NS,
                "account_id": route.account_id,
                "route_id": route.route_id,
                "journal_sequence": 0,
                "journal_state_hash": GENESIS_HASH,
                "scope": POST_WINDOW_SAFETY_SCOPE,
            },
        ),
        PublishedTargetCyclePayload(
            {"status": "direct-test"},
            publication=publication,
            route=route,
        ),
        sleeve="long",
    )
    path.chmod(0o600)


def _run(
    tmp_path: Path,
    route: AccountRoute,
    *,
    publisher: AccountTargetPublisher | None = None,
    now_ns: int = NOW_NS,
):
    capture, manifest = _paths(tmp_path)
    return publish_natural_safety_flatten(
        route=route,
        freeze_id="natural-v1",
        t1_ns=T1_NS,
        target_capture_path=capture,
        manifest_output_path=manifest,
        now_ns=now_ns,
        publisher=publisher,
    )


def test_refuses_before_t1_without_creating_outputs(tmp_path: Path) -> None:
    route = _route(tmp_path)
    kernel = _kernel(route)
    _publish_health(kernel, route)
    capture, manifest = _paths(tmp_path)

    with pytest.raises(RuntimeError, match="refuses before T1"):
        _run(tmp_path, route, now_ns=T1_NS - 1)

    assert not capture.exists()
    assert not manifest.exists()


def test_publishes_every_active_component_as_exact_risk_zero_and_manifests(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path)
    kernel = _kernel(route)
    long_key = _open_component(
        kernel,
        sleeve=SleeveAdapterKind.LONG,
        strategy_id="long-natural",
        component_id="alpha",
        symbol="BTCUSDT",
        signed_qty=1.0,
    )
    continuous_key = _open_component(
        kernel,
        sleeve=SleeveAdapterKind.CONTINUOUS,
        strategy_id="continuous-natural",
        component_id="beta",
        symbol="ETHUSDT",
        signed_qty=-2.0,
    )
    _publish_health(kernel, route)

    result = _run(tmp_path, route)

    assert result.passed and not result.already_flat
    assert result.active_component_count == 2
    assert len(result.published_request_ids) == 2
    requests = AccountIntentInbox(route).unresolved_requests()
    assert {item.intent.target_key for request in requests for item in request.intents} == {
        long_key,
        continuous_key,
    }
    for request in requests:
        assert request.batch_id.startswith("natural-safety-flatten/natural-v1/")
        assert request.created_ts_ns >= T1_NS
        assert len(request.intents) == 1
        item = request.intents[0]
        assert item.adapter_kind == SleeveAdapterKind.RISK
        assert item.intent.signed_notional_usdt == 0.0
        assert dict(item.intent.metadata) == {
            "natural_safety_flatten": True,
            "natural_freeze_id": "natural-v1",
        }
    capture, manifest = _paths(tmp_path)
    assert stat.S_IMODE(capture.stat().st_mode) == 0o600
    events, _hash = load_target_scheduling_capture(capture)
    assert len(events) == 2
    assert {event.sleeve for event in events} == {"long", "continuous"}
    assert all(event.strategy_profile == SAFETY_STRATEGY_PROFILE for event in events)
    loaded = load_post_window_safety_manifest(
        manifest,
        target_capture_path=capture,
        expected_account_id=ACCOUNT_ID,
        expected_t1_ns=T1_NS,
    )
    assert loaded["request_ids"] == list(result.published_request_ids)
    assert loaded["route_id"] == route.route_id
    assert loaded["producer_profile"] == SAFETY_STRATEGY_PROFILE
    assert loaded["execution_authorization"] == "not_granted"


@pytest.mark.parametrize(
    ("sleeve", "target_key", "message"),
    [
        (SleeveAdapterKind.HEDGE, "hedge/hedge-natural/main/BTCUSDT", "unknown natural sleeve"),
        (SleeveAdapterKind.LONG, "long/wrong", "canonical identity"),
    ],
)
def test_unknown_or_malformed_active_desire_fails_closed(
    tmp_path: Path,
    sleeve: SleeveAdapterKind,
    target_key: str,
    message: str,
) -> None:
    route = _route(tmp_path)
    kernel = _kernel(route)
    _open_component(
        kernel,
        sleeve=sleeve,
        strategy_id="hedge-natural" if sleeve is SleeveAdapterKind.HEDGE else "long-natural",
        component_id="main",
        symbol="BTCUSDT",
        signed_qty=1.0,
        target_key=target_key,
    )
    _publish_health(kernel, route)

    with pytest.raises(ValueError, match=message):
        _run(tmp_path, route)

    assert not (tmp_path / "safety-manifest.json").exists()


def test_refuses_unresolved_request_and_working_order(tmp_path: Path) -> None:
    unresolved_root = tmp_path / "unresolved"
    unresolved_root.mkdir()
    route = _route(unresolved_root)
    kernel = _kernel(route)
    _publish_health(kernel, route)
    AccountTargetPublisher(route).publish(
        batch_id="unresolved",
        intents=(
            requested_target(
                adapter_kind=SleeveAdapterKind.LONG,
                decision_key="pending",
                target_key="long/pending/main/BTCUSDT",
                strategy_id="pending",
                component_id="main",
                symbol="BTCUSDT",
                signed_notional_usdt=1.0,
                leverage=1.0,
                reason="pending",
            ),
        ),
        created_ts_ns=NOW_NS,
    )
    with pytest.raises(RuntimeError, match="requests are unresolved"):
        _run(unresolved_root, route)

    working_root = tmp_path / "working"
    working_root.mkdir()
    working_route = _route(working_root)
    working_kernel = _kernel(working_route)
    result = working_kernel.submit_targets(
        batch_id="working",
        market_inputs=[_market("BTCUSDT")],
        targets=[
            DesiredTarget(
                decision_key="working",
                target_key="long/working/main/BTCUSDT",
                sleeve="long",
                strategy_id="working",
                component_id="main",
                symbol="BTCUSDT",
                signed_qty=1.0,
                reference_price=100.0,
                leverage=1.0,
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules={"BTCUSDT": _rules("BTCUSDT")},
    )
    assert result.commands
    _publish_health(working_kernel, working_route)
    with pytest.raises(RuntimeError, match="orders are working"):
        _run(working_root, working_route)


class _FailSecondPublisher(AccountTargetPublisher):
    def __init__(self, route: AccountRoute) -> None:
        super().__init__(route)
        self.calls = 0

    def publish(
        self,
        *,
        batch_id: str,
        intents: Sequence[RequestedIntent],
        created_ts_ns: int | None = None,
    ) -> PublishedTargetRequest:
        self.calls += 1
        if self.calls == 2:
            raise OSError("injected second publication failure")
        return super().publish(
            batch_id=batch_id,
            intents=intents,
            created_ts_ns=created_ts_ns,
        )


def _consume_zero_request(
    route: AccountRoute,
    kernel: AccountExecutionKernel,
) -> str:
    inbox = AccountIntentInbox(route)
    claimed = inbox.claim_next()
    assert claimed is not None
    claimed_path, request = claimed
    item = request.intents[0]
    owner_sleeve = item.intent.target_key.split("/", 1)[0]
    symbol = item.intent.symbol
    result = kernel.submit_targets(
        batch_id=request.batch_id,
        market_inputs=[_market(symbol)],
        targets=[
            DesiredTarget(
                decision_key=item.intent.decision_key,
                target_key=item.intent.target_key,
                sleeve=owner_sleeve,
                strategy_id=item.intent.strategy_id,
                component_id=item.intent.component_id,
                symbol=symbol,
                signed_qty=0.0,
                reference_price=100.0,
                leverage=item.intent.leverage,
                reason=item.intent.reason,
                metadata=dict(item.intent.metadata),
            )
        ],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules={symbol: _rules(symbol)},
        require_strict_risk_reduction=True,
        request_content_hash=request.content_hash(),
    )
    assert result.accepted and len(result.commands) == 1
    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id=f"close:{command.command_id}",
        exchange_ts_ns=1_300_000_000,
        local_ack_ts_ns=1_300_000_001,
    )
    kernel.record_fill(
        command_id=command.command_id,
        execution_id=f"close-fill:{command.command_id}",
        signed_qty=command.signed_qty,
        price=100.0,
        fee_usdt=0.01,
        exchange_ts_ns=1_300_000_002,
        local_receive_ts_ns=1_300_000_003,
    )
    state = kernel.state()
    inbox.complete(
        claimed_path,
        AccountServiceReceipt(
            request_id=request.request_id,
            request_hash=request.content_hash(),
            batch_id=request.batch_id,
            accepted=True,
            rejection_keys=(),
            command_ids=(command.command_id,),
            execution_event_ids=(),
            final_state_hash=state.rolling_state_hash,
        ),
    )
    return item.intent.target_key


def test_partial_publication_has_no_manifest_but_retry_is_idempotent(tmp_path: Path) -> None:
    route = _route(tmp_path)
    kernel = _kernel(route)
    keys = {
        _open_component(
            kernel,
            sleeve=SleeveAdapterKind.LONG,
            strategy_id="long-natural",
            component_id="one",
            symbol="BTCUSDT",
            signed_qty=1.0,
        ),
        _open_component(
            kernel,
            sleeve=SleeveAdapterKind.CONTINUOUS,
            strategy_id="continuous-natural",
            component_id="two",
            symbol="ETHUSDT",
            signed_qty=-2.0,
        ),
    }
    _publish_health(kernel, route)

    failed = _run(tmp_path, route, publisher=_FailSecondPublisher(route))

    assert not failed.passed
    assert len(failed.published_request_ids) == 1
    assert len(failed.errors) == 1
    assert failed.manifest_path is None
    assert not (tmp_path / "safety-manifest.json").exists()
    capture, _manifest = _paths(tmp_path)
    first_events, _hash = load_target_scheduling_capture(capture)
    assert len(first_events) == 1
    first_key = _consume_zero_request(route, kernel)
    _publish_health(kernel, route)

    retried = _run(tmp_path, route)

    assert retried.passed
    assert retried.active_component_count == 1
    assert first_key not in {
        item.intent.target_key
        for request in AccountIntentInbox(route).unresolved_requests()
        for item in request.intents
    }
    all_events, _hash = load_target_scheduling_capture(capture)
    captured_keys = {
        item.intent.target_key
        for event in all_events
        for request in event.requests
        for item in request.request.intents
    }
    assert captured_keys == keys
    assert len(all_events) == 2


def test_already_flat_writes_one_explicit_empty_capture_and_manifest(tmp_path: Path) -> None:
    route = _route(tmp_path)
    kernel = _kernel(route)
    _publish_health(kernel, route)

    result = _run(tmp_path, route)

    assert result.passed and result.already_flat
    assert result.published_request_ids == ()
    events, _hash = load_target_scheduling_capture(tmp_path / "safety-targets.jsonl")
    assert len(events) == 1
    assert events[0].requests == ()
    manifest = load_post_window_safety_manifest(
        tmp_path / "safety-manifest.json",
        target_capture_path=tmp_path / "safety-targets.jsonl",
        expected_account_id=ACCOUNT_ID,
        expected_t1_ns=T1_NS,
    )
    assert manifest["successful_empty_event_count"] == 1
    assert manifest["request_ids"] == []


def test_manifest_destination_is_create_only_before_any_second_publication(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path)
    kernel = _kernel(route)
    _publish_health(kernel, route)
    first = _run(tmp_path, route)
    assert first.passed
    events_before, hash_before = load_target_scheduling_capture(
        tmp_path / "safety-targets.jsonl"
    )

    with pytest.raises(FileExistsError, match="manifest already exists"):
        _run(tmp_path, route)

    events_after, hash_after = load_target_scheduling_capture(
        tmp_path / "safety-targets.jsonl"
    )
    assert events_after == events_before
    assert hash_after == hash_before


def test_low_level_manifest_rejects_wrong_reserved_producer_profile(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path)
    capture = tmp_path / "wrong-profile.jsonl"
    _write_direct_safety_capture(
        capture,
        route=route,
        publication=ExitFirstPublication((), (), ()),
        profile="ordinary-long-profile",
    )

    with pytest.raises(ValueError, match="reserved producer"):
        build_post_window_safety_manifest(
            target_capture_path=capture,
            expected_account_id=route.account_id,
            freeze_id="natural-v1",
            t1_ns=T1_NS,
            output_path=tmp_path / "wrong-profile-manifest.json",
        )


def test_low_level_manifest_rejects_non_risk_zero_request(tmp_path: Path) -> None:
    route = _route(tmp_path)
    created_ts = T1_NS + 1
    target_key = "long/safety/main/BTCUSDT"
    digest = hashlib.sha256(target_key.encode()).hexdigest()[:16]
    batch_id = f"natural-safety-flatten/natural-v1/{created_ts}/0000/{digest}"
    target = requested_target(
        adapter_kind=SleeveAdapterKind.LONG,
        decision_key=f"{batch_id}/zero",
        target_key=target_key,
        strategy_id="safety",
        component_id="main",
        symbol="BTCUSDT",
        signed_notional_usdt=0.0,
        leverage=1.0,
        reason=SAFETY_REASON,
        metadata={
            "natural_safety_flatten": True,
            "natural_freeze_id": "natural-v1",
        },
    )
    receipt = AccountTargetPublisher(route).publish(
        batch_id=batch_id,
        intents=(target,),
        created_ts_ns=created_ts,
    )
    capture = tmp_path / "non-risk.jsonl"
    _write_direct_safety_capture(
        capture,
        route=route,
        publication=ExitFirstPublication((receipt,), (), ()),
        event_ts_ns=created_ts,
    )

    with pytest.raises(ValueError, match="RISK adapter"):
        build_post_window_safety_manifest(
            target_capture_path=capture,
            expected_account_id=route.account_id,
            freeze_id="natural-v1",
            t1_ns=T1_NS,
            output_path=tmp_path / "non-risk-manifest.json",
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("metadata", "exact safety metadata"),
        ("target", "canonical component"),
        ("batch", "reserved batch identity"),
        ("decision", "decision identity"),
    ],
)
def test_low_level_manifest_rejects_loose_target_identity(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    route = _route(tmp_path)
    created_ts = T1_NS + 1
    target_key = (
        "long/invented"
        if case == "target"
        else "long/safety/main/BTCUSDT"
    )
    digest = hashlib.sha256(target_key.encode()).hexdigest()[:16]
    batch_id = (
        "natural-safety-flatten/natural-v1/malformed"
        if case == "batch"
        else f"natural-safety-flatten/natural-v1/{created_ts}/0000/{digest}"
    )
    metadata: dict[str, object] = {
        "natural_safety_flatten": True,
        "natural_freeze_id": "natural-v1",
    }
    if case == "metadata":
        metadata["extra"] = "not-registered"
    decision_key = f"{batch_id}/wrong" if case == "decision" else f"{batch_id}/zero"
    target = requested_target(
        adapter_kind=SleeveAdapterKind.RISK,
        decision_key=decision_key,
        target_key=target_key,
        strategy_id="safety",
        component_id="main",
        symbol="BTCUSDT",
        signed_notional_usdt=0.0,
        leverage=1.0,
        reason=SAFETY_REASON,
        metadata=metadata,
    )
    receipt = AccountTargetPublisher(route).publish(
        batch_id=batch_id,
        intents=(target,),
        created_ts_ns=created_ts,
    )
    capture = tmp_path / f"loose-{case}.jsonl"
    _write_direct_safety_capture(
        capture,
        route=route,
        publication=ExitFirstPublication((receipt,), (), ()),
        event_ts_ns=created_ts,
    )

    with pytest.raises(ValueError, match=message):
        build_post_window_safety_manifest(
            target_capture_path=capture,
            expected_account_id=route.account_id,
            freeze_id="natural-v1",
            t1_ns=T1_NS,
            output_path=tmp_path / f"loose-{case}-manifest.json",
        )


def test_low_level_manifest_requires_mode_0600_target_capture(tmp_path: Path) -> None:
    route = _route(tmp_path)
    capture = tmp_path / "loose-mode.jsonl"
    _write_direct_safety_capture(
        capture,
        route=route,
        publication=ExitFirstPublication((), (), ()),
    )
    capture.chmod(0o644)

    with pytest.raises(ValueError, match="target capture must be mode 0600"):
        build_post_window_safety_manifest(
            target_capture_path=capture,
            expected_account_id=route.account_id,
            freeze_id="natural-v1",
            t1_ns=T1_NS,
            output_path=tmp_path / "loose-mode-manifest.json",
        )


def test_active_component_helper_rejects_unknown_payload_fields() -> None:
    from liquidity_migration.account_kernel import AccountState

    state = AccountState(
        component_target_desires={
            "long/test/main/BTCUSDT": {
                "target_key": "long/test/main/BTCUSDT",
                "unknown": True,
            }
        }
    )
    with pytest.raises(ValueError, match="unknown or missing fields"):
        active_component_desires(state)
