from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.account.account_intent_client import (
    AccountTargetPublisher,
    ExitFirstPublication,
    TargetPublicationError,
    component_target_key,
    publish_exit_first_target_requests,
    requested_target,
)
from liquidity_migration.account.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account.account_service import AccountIntentInbox, SleeveAdapterKind
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.strategy.continuous_demo import ContinuousDemoCycleConfig
from liquidity_migration.strategy.continuous_demo_daemon import ContinuousDemoDaemon
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.strategy.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.strategy.long_native_event_demo_daemon import (
    LongNativeDemoDaemon,
)
from liquidity_migration.account.strategy_event_clock import JsonlStrategyEventTape, StrategyEvent
from liquidity_migration.strategy.strategy_event_outcome import (
    JsonlStrategyEventDecisionTape,
    load_strategy_event_decision_tape,
)
from liquidity_migration.strategy.strategy_cycle_health import (
    read_strategy_cycle_health,
    strategy_cycle_health_path,
)
from liquidity_migration.strategy.strategy_target_replay import (
    JsonlTargetSchedulingCaptureTape,
    PublishedTargetCyclePayload,
    capture_event_from_cycle,
    load_target_scheduling_capture,
)


def _route(tmp_path: Path, *, environment: str = "demo") -> AccountRoute:
    return ensure_account_route(
        account_id=f"capture-{environment}-account",
        environment=environment,
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _entry_intent(*, sleeve: SleeveAdapterKind = SleeveAdapterKind.LONG, suffix: str = "a"):
    strategy_id = "capture_strategy"
    target_key = component_target_key(
        sleeve=sleeve,
        strategy_id=strategy_id,
        component_id=suffix,
        symbol="BTCUSDT",
    )
    return requested_target(
        adapter_kind=sleeve,
        decision_key=f"{sleeve.value}-target/cycle/entry/{suffix}",
        target_key=target_key,
        strategy_id=strategy_id,
        component_id=suffix,
        symbol="BTCUSDT",
        signed_notional_usdt=100.0,
        leverage=2.0,
        reason="captured_entry",
    )


def _published_cycle(
    route: AccountRoute,
    *,
    with_entry: bool = True,
    sleeve: SleeveAdapterKind = SleeveAdapterKind.LONG,
    payload: dict[str, Any] | None = None,
) -> PublishedTargetCyclePayload:
    publisher = AccountTargetPublisher(route, clock=VirtualClock(current_wall_ns=1_000_000_000))
    publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix=f"{sleeve.value}-target/capture-cycle",
        exit_intents=(),
        entry_intents=(_entry_intent(sleeve=sleeve),) if with_entry else (),
        created_ts_ns=1_000_000_000,
    )
    return PublishedTargetCyclePayload(
        payload or {"cycle": {"cycle_id": "capture", "mode": "demo_target"}},
        publication=publication,
        route=route,
    )


def _event(sequence: int, *, sleeve: str = "long", timestamp: int | None = None) -> StrategyEvent:
    return StrategyEvent(
        event_ts_ns=timestamp or sequence * 1_000_000_000,
        ingest_ts_ns=(timestamp or sequence * 1_000_000_000) + 10,
        source=f"{sleeve}:demo",
        source_sequence=sequence,
        kind="startup" if sequence == 1 else "timer",
        payload={
            "execution_environment": "demo",
            "strategy_profile": {
                "long": "LongV11aDivWeekendVol",
                "carry": "lane2_carry_hold_v3",
            }.get(sleeve, "continuous_ensemble_v2"),
        },
    )


def test_capture_is_built_from_verified_durable_requests_and_explicit_empty_cycles(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path)
    capture_path = tmp_path / "natural-target-capture.jsonl"
    tape = JsonlTargetSchedulingCaptureTape(capture_path)
    first_payload = _published_cycle(route)
    claimed = AccountIntentInbox(route).claim_next()
    assert claimed is not None

    first = tape.append_from_cycle(
        _event(1),
        first_payload,
        sleeve="long",
    )
    second = tape.append_from_cycle(
        _event(2),
        _published_cycle(route, with_entry=False),
        sleeve="long",
    )

    assert len(first.requests) == 1
    assert first.requests[0].request.to_dict()["intents"][0]["intent"]["decision_key"] == ("long-target/cycle/entry/a")
    assert first.decision_keys == ("long-target/cycle/entry/a",)
    assert first.requests[0].arrival_sequence > 0
    assert first.requests[0].durable_queue_state == "processing"
    assert second.requests == ()
    assert second.decision_keys == ()
    loaded, chain_hash = load_target_scheduling_capture(capture_path)
    assert loaded == (first, second)
    assert len(chain_hash) == 64
    assert capture_path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(ValueError, match="duplicate"):
        tape.append_from_cycle(_event(1), _published_cycle(route), sleeve="long")


def test_capture_accepts_the_carry_sleeve(tmp_path: Path) -> None:
    """The capture sleeve set includes carry, so carry producer cycles are
    first-class capture evidence rather than a validation reject."""

    route = _route(tmp_path)
    capture_path = tmp_path / "carry-target-capture.jsonl"
    tape = JsonlTargetSchedulingCaptureTape(capture_path)
    payload = _published_cycle(route, sleeve=SleeveAdapterKind.CARRY)
    claimed = AccountIntentInbox(route).claim_next()
    assert claimed is not None

    outcome = tape.append_from_cycle(
        _event(1, sleeve="carry"),
        payload,
        sleeve="carry",
    )

    assert outcome.sleeve == "carry"
    assert outcome.decision_keys == ("carry-target/cycle/entry/a",)
    loaded, _chain_hash = load_target_scheduling_capture(capture_path)
    assert loaded == (outcome,)


def test_strategy_outcome_tape_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.jsonl"
    event = _event(1)
    JsonlStrategyEventDecisionTape(path).append(event.event_id, ())

    assert path.stat().st_mode & 0o777 == 0o600
    assert load_strategy_event_decision_tape(path)[0][0].event_id == event.event_id


def test_publication_error_or_missing_durable_request_leaves_capture_unavailable(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path)
    good = _published_cycle(route)
    failed = PublishedTargetCyclePayload(
        good,
        publication=ExitFirstPublication(
            exit_requests=good.publication.exit_requests,
            entry_requests=good.publication.entry_requests,
            errors=(
                TargetPublicationError(
                    stage="entry",
                    target_key="long/capture_strategy/a/BTCUSDT",
                    error_type="OSError",
                    message="transport failed",
                ),
            ),
        ),
        route=route,
    )
    with pytest.raises(ValueError, match="publication contains errors"):
        capture_event_from_cycle(_event(1), failed, sleeve="long")

    published = good.publication.entry_requests[0]
    published.path.unlink()
    with pytest.raises(RuntimeError, match="0 durable files"):
        capture_event_from_cycle(_event(1), good, sleeve="long")


def test_capture_tape_rejects_tamper(tmp_path: Path) -> None:
    route = _route(tmp_path)
    capture_path = tmp_path / "capture.jsonl"
    JsonlTargetSchedulingCaptureTape(capture_path).append_from_cycle(
        _event(1),
        _published_cycle(route),
        sleeve="long",
    )
    row = json.loads(capture_path.read_text())
    row["capture_event"]["decision_keys"] = ["changed"]
    capture_path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="decision keys|hash mismatch"):
        load_target_scheduling_capture(capture_path)


def test_long_daemon_appends_post_callback_outcome_only_from_durable_publication(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path / "route")
    result = _published_cycle(route)
    root = tmp_path / "long"
    daemon = LongNativeDemoDaemon(
        root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=route.inbox_root,
            account_execution_root=route.account_root,
            ws_klines_enabled=False,
        ),
        interval_seconds=0.0,
        cycle_runner=lambda *_args, **_kwargs: result,
        clock=VirtualClock(current_wall_ns=1_000_000_000),
    )

    daemon._run_one_cycle()

    events = JsonlStrategyEventTape(root / "strategy_event_tape.jsonl").prior_events
    outcomes, _ = load_strategy_event_decision_tape(root / "strategy_event_decision_tape.jsonl")
    captures, _ = load_target_scheduling_capture(root / "strategy_target_scheduling_capture.jsonl")
    assert len(events) == len(outcomes) == len(captures) == 1
    assert outcomes[0].event_id == events[0].event_id
    assert outcomes[0].decision_keys == ("long-target/cycle/entry/a",)
    assert captures[0].source_event.event_id == events[0].event_id


def test_daemon_callback_or_publication_failure_leaves_outcome_missing(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path / "route")

    def crash(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("callback failed")

    callback_root = tmp_path / "callback-failure"
    callback_daemon = LongNativeDemoDaemon(
        callback_root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=route.inbox_root,
            account_execution_root=route.account_root,
            ws_klines_enabled=False,
        ),
        cycle_runner=crash,
        clock=VirtualClock(current_wall_ns=1_000_000_000),
    )
    callback_daemon._run_one_cycle()
    assert len(JsonlStrategyEventTape(callback_root / "strategy_event_tape.jsonl").prior_events) == 1
    assert load_strategy_event_decision_tape(callback_root / "strategy_event_decision_tape.jsonl")[0] == ()

    successful = _published_cycle(route)
    failed_result = PublishedTargetCyclePayload(
        successful,
        publication=ExitFirstPublication(
            exit_requests=(),
            entry_requests=successful.publication.entry_requests,
            errors=(TargetPublicationError("entry", "", "OSError", "failed"),),
        ),
        route=route,
    )
    publication_root = tmp_path / "publication-failure"
    publication_daemon = LongNativeDemoDaemon(
        publication_root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=route.inbox_root,
            account_execution_root=route.account_root,
            ws_klines_enabled=False,
        ),
        cycle_runner=lambda *_args, **_kwargs: failed_result,
        clock=VirtualClock(current_wall_ns=2_000_000_000),
    )
    publication_daemon._run_one_cycle()
    assert load_strategy_event_decision_tape(publication_root / "strategy_event_decision_tape.jsonl")[0] == ()
    assert load_target_scheduling_capture(publication_root / "strategy_target_scheduling_capture.jsonl")[0] == ()
    assert publication_daemon._strategy_evidence_errors == 1


def test_continuous_daemon_records_successful_no_target_cycle(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path / "route")
    result = PublishedTargetCyclePayload(
        {
            "cycle_id": "empty",
            "mode": "demo_target",
            "universe_symbols": 0,
            "rmom_present": False,
            "live_d9_symbols": 0,
            "candidates": 0,
            "entries": 0,
            "exits": 0,
            "open_positions": 0,
            "equity_usdt": 0.0,
        },
        publication=ExitFirstPublication((), (), ()),
        route=route,
    )
    root = tmp_path / "continuous"
    daemon = ContinuousDemoDaemon(
        root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=ContinuousDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=route.inbox_root,
            account_execution_root=route.account_root,
            ws_klines_enabled=False,
        ),
        cycle_runner=lambda *_args, **_kwargs: result,
        clock=VirtualClock(current_wall_ns=1_000_000_000),
    )

    daemon._run_one_cycle()

    outcomes, _ = load_strategy_event_decision_tape(root / "strategy_event_decision_tape.jsonl")
    captures, _ = load_target_scheduling_capture(root / "strategy_target_scheduling_capture.jsonl")
    assert outcomes[0].decision_keys == ()
    assert captures[0].sleeve == "continuous"
    assert captures[0].requests == ()


def test_continuous_daemon_publishes_post_evidence_completion_health(
    tmp_path: Path,
) -> None:
    invocation_id = "34" * 16
    route = _route(tmp_path / "route", environment="paper")
    result = PublishedTargetCyclePayload(
        {
            "cycle_id": "continuous-target-health-1000",
            "ts_ms": 1_000,
            "mode": "paper_target",
            "universe_symbols": 10,
        },
        publication=ExitFirstPublication((), (), ()),
        route=route,
    )

    class _KlineManager:
        def store(self) -> None:
            return None

        def stats(self) -> dict[str, object]:
            return {"store": {"rows": 383_711}}

    root = tmp_path / "continuous-paper"
    daemon = ContinuousDemoDaemon(
        root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=ContinuousDemoCycleConfig(
            execution_environment="paper",
            account_intent_inbox_root=route.inbox_root,
            account_execution_root=route.account_root,
            ws_klines_enabled=False,
        ),
        cycle_runner=lambda *_args, **_kwargs: result,
        kline_stream_manager=_KlineManager(),
        clock=VirtualClock(current_wall_ns=1_000_000_000),
        strategy_invocation_id=invocation_id,
        completion_clock_ns=lambda: 2_000_000_000,
    )

    daemon._run_one_cycle()

    health = read_strategy_cycle_health(root)
    assert health.cycle_id == "continuous-target-health-1000"
    assert health.cycle_ts_ms == 1_000
    assert health.completed_ts_ns == 2_000_000_000
    assert health.invocation_id == invocation_id
    assert health.environment == "paper"
    assert health.ws_kline_store_rows == 383_711
    assert (
        load_strategy_event_decision_tape(root / "strategy_event_decision_tape.jsonl")[0][0].event_id
        == JsonlStrategyEventTape(root / "strategy_event_tape.jsonl").prior_events[0].event_id
    )


def test_completion_health_never_precedes_evidence_and_cannot_invalidate_it(
    tmp_path: Path,
) -> None:
    invocation_id = "56" * 16
    route = _route(tmp_path / "route")
    successful = _published_cycle(
        route,
        with_entry=False,
        payload={
            "cycle": {
                "cycle_id": "long-target-health-1000",
                "ts_ms": 1_000,
                "mode": "demo_target",
            }
        },
    )
    failed_publication = PublishedTargetCyclePayload(
        successful,
        publication=ExitFirstPublication(
            (),
            (),
            (TargetPublicationError("entry", "", "OSError", "failed"),),
        ),
        route=route,
    )
    evidence_failure_root = tmp_path / "evidence-failure"
    evidence_failure = LongNativeDemoDaemon(
        evidence_failure_root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=route.inbox_root,
            account_execution_root=route.account_root,
            ws_klines_enabled=False,
        ),
        cycle_runner=lambda *_args, **_kwargs: failed_publication,
        strategy_invocation_id=invocation_id,
    )

    evidence_failure._run_one_cycle()

    assert evidence_failure._strategy_evidence_errors == 1
    assert not strategy_cycle_health_path(evidence_failure_root).exists()

    projection_failure_root = tmp_path / "projection-failure"
    projection_failure = LongNativeDemoDaemon(
        projection_failure_root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=route.inbox_root,
            account_execution_root=route.account_root,
            ws_klines_enabled=False,
        ),
        cycle_runner=lambda *_args, **_kwargs: successful,
        strategy_invocation_id=invocation_id,
        completion_clock_ns=lambda: 0,
    )

    projection_failure._run_one_cycle()

    assert projection_failure._strategy_evidence_errors == 0
    assert projection_failure._strategy_health_errors == 1
    assert (
        len(load_strategy_event_decision_tape(projection_failure_root / "strategy_event_decision_tape.jsonl")[0]) == 1
    )
    assert not strategy_cycle_health_path(projection_failure_root).exists()
