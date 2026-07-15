from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.long_native_event_demo_daemon as daemon_module
import liquidity_migration.strategy_target_replay as replay_module
from liquidity_migration.account_intent_client import (
    AccountTargetPublisher,
    ExitFirstPublication,
    TargetPublicationError,
    component_target_key,
    publish_exit_first_target_requests,
    requested_target,
)
from liquidity_migration.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account_service import AccountIntentInbox, SleeveAdapterKind
from liquidity_migration.config import ResearchConfig
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig
from liquidity_migration.continuous_demo_daemon import ContinuousDemoDaemon
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.long_native_event_demo_daemon import (
    LongNativeDemoDaemon,
    StrategyEvidenceEpochError,
)
from liquidity_migration.natural_run_config import (
    NaturalRunConfig,
    NaturalSleeveRuntime,
)
from liquidity_migration.strategy_event_clock import JsonlStrategyEventTape, StrategyEvent
from liquidity_migration.strategy_event_outcome import (
    JsonlStrategyEventDecisionTape,
    load_strategy_event_decision_tape,
)
from liquidity_migration.strategy_event_parity import build_strategy_event_parity_receipt
from liquidity_migration.strategy_target_replay import (
    JsonlTargetSchedulingCaptureTape,
    PublishedTargetCyclePayload,
    capture_event_from_cycle,
    load_offline_target_scheduling_replay_manifest,
    load_target_scheduling_capture,
    run_offline_target_scheduling_replay,
)


def _route(tmp_path: Path, *, environment: str = "demo") -> AccountRoute:
    return ensure_account_route(
        account_id=f"capture-{environment}-account",
        environment=environment,
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _natural_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    candidate: Path,
    capture: Path,
) -> Path:
    config_path = root / "natural-run-config.json"
    config = NaturalRunConfig(
        path=config_path,
        freeze_manifest_path=root / "freeze.json",
        freeze_manifest_file_sha256="a" * 64,
        freeze_artifact_sha256="b" * 64,
        freeze_id=f"natural-cutover-{'c' * 64}",
        repository_root=root,
        candidate_universe_path=candidate,
        candidate_universe_file_sha256="d" * 64,
        t0_ns=1,
        t1_ns=10**20,
        target_capture_path=capture,
        sleeves={
            "long": NaturalSleeveRuntime(
                data_root=root,
                event_tape_path=root / "strategy_event_tape.jsonl",
                outcome_tape_path=root / "strategy_event_decision_tape.jsonl",
            ),
            "continuous": NaturalSleeveRuntime(
                data_root=root,
                event_tape_path=root / "strategy_event_tape.jsonl",
                outcome_tape_path=root / "strategy_event_decision_tape.jsonl",
            ),
        },
        artifact_sha256="e" * 64,
    )
    monkeypatch.setattr(daemon_module, "load_natural_run_config", lambda _path: config)
    return config_path


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
            "strategy_profile": (
                "LongV11aDivWeekendVol" if sleeve == "long" else "continuous_ensemble_v2"
            ),
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
    assert first.requests[0].request.to_dict()["intents"][0]["intent"]["decision_key"] == (
        "long-target/cycle/entry/a"
    )
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
    outcomes, _ = load_strategy_event_decision_tape(
        root / "strategy_event_decision_tape.jsonl"
    )
    captures, _ = load_target_scheduling_capture(
        root / "strategy_target_scheduling_capture.jsonl"
    )
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
    assert load_strategy_event_decision_tape(
        callback_root / "strategy_event_decision_tape.jsonl"
    )[0] == ()

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
    assert load_strategy_event_decision_tape(
        publication_root / "strategy_event_decision_tape.jsonl"
    )[0] == ()
    assert load_target_scheduling_capture(
        publication_root / "strategy_target_scheduling_capture.jsonl"
    )[0] == ()
    assert publication_daemon._strategy_evidence_errors == 1


def test_natural_evidence_mode_stops_after_callback_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = _route(tmp_path / "route")

    def crash(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("callback failed")

    root = tmp_path / "natural-callback-failure"
    candidate = tmp_path / "candidate.json"
    capture = root / "shared-capture.jsonl"
    config_path = _natural_runtime_config(
        monkeypatch,
        root=root,
        candidate=candidate,
        capture=capture,
    )
    daemon = LongNativeDemoDaemon(
        root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=route.inbox_root,
            account_execution_root=route.account_root,
            candidate_universe_file=str(candidate),
            ws_klines_enabled=False,
        ),
        cycle_runner=crash,
        clock=VirtualClock(current_wall_ns=1_000_000_000),
        strategy_target_capture_path=capture,
        natural_evidence_required=True,
        natural_run_config_path=config_path,
    )

    with pytest.raises(StrategyEvidenceEpochError, match="callback failed"):
        daemon._run_one_cycle()
    assert len(JsonlStrategyEventTape(root / "strategy_event_tape.jsonl").prior_events) == 1
    assert load_strategy_event_decision_tape(
        root / "strategy_event_decision_tape.jsonl"
    )[0] == ()


def test_natural_evidence_mode_stops_after_capture_or_outcome_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = _route(tmp_path / "route")
    result = _published_cycle(route)

    class FailingCapture:
        def append_from_cycle(self, *_args: Any, **_kwargs: Any) -> None:
            raise OSError("capture fsync failed")

    common_config = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_intent_inbox_root=route.inbox_root,
        account_execution_root=route.account_root,
        candidate_universe_file=str(tmp_path / "candidate.json"),
        ws_klines_enabled=False,
    )
    capture_root = tmp_path / "natural-capture-failure"
    capture_path = capture_root / "shared-capture.jsonl"
    capture_config_path = _natural_runtime_config(
        monkeypatch,
        root=capture_root,
        candidate=tmp_path / "candidate.json",
        capture=capture_path,
    )
    capture_daemon = LongNativeDemoDaemon(
        capture_root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=common_config,
        cycle_runner=lambda *_args, **_kwargs: result,
        clock=VirtualClock(current_wall_ns=2_000_000_000),
        strategy_target_capture_recorder=FailingCapture(),  # type: ignore[arg-type]
        natural_evidence_required=True,
        natural_run_config_path=capture_config_path,
    )
    with pytest.raises(StrategyEvidenceEpochError, match="capture/outcome"):
        capture_daemon._run_one_cycle()

    class FailingOutcome:
        def append(self, *_args: Any, **_kwargs: Any) -> None:
            raise OSError("outcome fsync failed")

    outcome_root = tmp_path / "natural-outcome-failure"
    outcome_capture = outcome_root / "shared-capture.jsonl"
    outcome_config_path = _natural_runtime_config(
        monkeypatch,
        root=outcome_root,
        candidate=tmp_path / "candidate.json",
        capture=outcome_capture,
    )
    outcome_daemon = LongNativeDemoDaemon(
        outcome_root,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=common_config,
        cycle_runner=lambda *_args, **_kwargs: result,
        clock=VirtualClock(current_wall_ns=3_000_000_000),
        strategy_decision_recorder=FailingOutcome(),  # type: ignore[arg-type]
        strategy_target_capture_path=outcome_capture,
        natural_evidence_required=True,
        natural_run_config_path=outcome_config_path,
    )
    with pytest.raises(StrategyEvidenceEpochError, match="capture/outcome"):
        outcome_daemon._run_one_cycle()
    captures, _ = load_target_scheduling_capture(
        outcome_capture
    )
    assert len(captures) == 1
    assert load_strategy_event_decision_tape(
        outcome_root / "strategy_event_decision_tape.jsonl"
    )[0] == ()


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

    outcomes, _ = load_strategy_event_decision_tape(
        root / "strategy_event_decision_tape.jsonl"
    )
    captures, _ = load_target_scheduling_capture(
        root / "strategy_target_scheduling_capture.jsonl"
    )
    assert outcomes[0].decision_keys == ()
    assert captures[0].sleeve == "continuous"
    assert captures[0].requests == ()


def test_offline_replay_emits_three_comparator_ready_isolated_tape_sets(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path / "route")
    capture_path = tmp_path / "frozen-capture.jsonl"
    capture = JsonlTargetSchedulingCaptureTape(capture_path)
    capture.append_from_cycle(_event(1), _published_cycle(route), sleeve="long")
    capture.append_from_cycle(
        _event(1, sleeve="continuous", timestamp=1_500_000_000),
        _published_cycle(route, sleeve=SleeveAdapterKind.CONTINUOUS),
        sleeve="continuous",
    )
    capture.append_from_cycle(
        _event(2),
        _published_cycle(route, with_entry=False),
        sleeve="long",
    )
    frozen_bytes = capture_path.read_bytes()
    output_root = tmp_path / "offline-replay"

    manifest = run_offline_target_scheduling_replay(
        capture_path,
        output_root=output_root,
    )

    assert manifest["evidence_scope"] == "captured_account_target_scheduling_only"
    assert manifest["schema_version"] == 2
    assert manifest["created_ts_ns"] > 0
    assert manifest["source_capture"]["capture_event_count"] == 3
    assert set(manifest["source_capture"]) >= {
        "device",
        "inode",
        "mtime_ns",
        "mode",
        "uid",
        "nlink",
    }
    assert (
        load_offline_target_scheduling_replay_manifest(
            output_root / "replay_manifest.json"
        )
        == manifest
    )
    event_tapes = {
        environment: output_root / environment / "strategy_event_tape.jsonl"
        for environment in ("historical", "paper", "demo")
    }
    decision_tapes = {
        environment: output_root / environment / "strategy_event_decision_tape.jsonl"
        for environment in ("historical", "paper", "demo")
    }
    replay_inputs = {
        environment: output_root / environment / "replay_input.jsonl"
        for environment in ("historical", "paper", "demo")
    }
    assert all(path.read_bytes() == frozen_bytes for path in replay_inputs.values())
    assert all(
        len((output_root / environment / "scheduled_target_requests.jsonl").read_text().splitlines()) == 3
        for environment in ("historical", "paper", "demo")
    )
    parity = build_strategy_event_parity_receipt(
        event_tapes,
        decision_tapes=decision_tapes,
        replay_inputs=replay_inputs,
        source_normalizations={
            environment: {
                f"long:{environment}": "long:replay",
                f"continuous:{environment}": "continuous:replay",
            }
            for environment in ("historical", "paper", "demo")
        },
        replay_manifest=output_root / "replay_manifest.json",
    )
    assert parity["strategy_event_replay_gate_passed"] is True
    assert parity["replay_provenance"]["deployment_valid"] is True
    assert parity["replay_provenance"]["canonical_source_capture"] == manifest[
        "source_capture"
    ]
    assert any(
        outcome.decision_keys == ()
        for outcome in load_strategy_event_decision_tape(decision_tapes["demo"])[0]
    )
    assert not (output_root / "demo" / "account_route.json").exists()


def test_offline_replay_rejects_input_mutation_existing_or_account_route_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(tmp_path / "route")
    capture_path = tmp_path / "frozen-capture.jsonl"
    JsonlTargetSchedulingCaptureTape(capture_path).append_from_cycle(
        _event(1),
        _published_cycle(route),
        sleeve="long",
    )

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="must not already exist"):
        run_offline_target_scheduling_replay(capture_path, output_root=existing)
    with pytest.raises(ValueError, match="account route root"):
        run_offline_target_scheduling_replay(
            capture_path,
            output_root=route.account_path / "offline-child",
        )

    original_loader = replay_module.load_target_scheduling_capture_bytes
    called = False

    def mutating_loader(data: bytes):  # noqa: ANN202
        nonlocal called
        result = original_loader(data)
        if not called:
            called = True
            capture_path.write_bytes(capture_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(
        replay_module,
        "load_target_scheduling_capture_bytes",
        mutating_loader,
    )
    with pytest.raises(ValueError, match="changed while replay was running"):
        run_offline_target_scheduling_replay(
            capture_path,
            output_root=tmp_path / "mutated-output",
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        Path("historical/strategy_event_tape.jsonl"),
        Path("paper/strategy_event_decision_tape.jsonl"),
        Path("demo/scheduled_target_requests.jsonl"),
        Path("historical/replay_input.jsonl"),
    ),
)
def test_replay_manifest_loader_reopens_every_published_file(
    tmp_path: Path,
    relative_path: Path,
) -> None:
    route = _route(tmp_path / "route")
    capture_path = tmp_path / "frozen-capture.jsonl"
    JsonlTargetSchedulingCaptureTape(capture_path).append_from_cycle(
        _event(1),
        _published_cycle(route),
        sleeve="long",
    )
    output_root = tmp_path / "offline-replay"
    run_offline_target_scheduling_replay(capture_path, output_root=output_root)
    changed = output_root / relative_path
    changed.chmod(0o600)
    changed.write_bytes(changed.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="changed after replay publication"):
        load_offline_target_scheduling_replay_manifest(
            output_root / "replay_manifest.json"
        )


def test_replay_manifest_semantically_rejects_rehashed_schedule_forgery(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path / "route")
    capture_path = tmp_path / "frozen-capture.jsonl"
    JsonlTargetSchedulingCaptureTape(capture_path).append_from_cycle(
        _event(1),
        _published_cycle(route),
        sleeve="long",
    )
    output_root = tmp_path / "offline-replay"
    run_offline_target_scheduling_replay(capture_path, output_root=output_root)
    schedule_path = output_root / "demo" / "scheduled_target_requests.jsonl"
    row = json.loads(schedule_path.read_text(encoding="utf-8"))
    row["scheduled_event"]["source_event_id"] = "forged-source-event"
    row["schedule_hash"] = replay_module._schedule_hash(  # noqa: SLF001
        row["prior_schedule_hash"], row["scheduled_event"]
    )
    schedule_path.write_bytes(replay_module.canonical_json(row) + b"\n")

    manifest_path = output_root / "replay_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["environments"]["demo"]["scheduled_targets"] = {
        **replay_module._file_identity(schedule_path),  # noqa: SLF001
        "event_count": 1,
        "chain_hash": row["schedule_hash"],
    }
    manifest["artifact_sha256"] = replay_module._self_hash(manifest)  # noqa: SLF001
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    with pytest.raises(ValueError, match="schedules do not reproduce"):
        load_offline_target_scheduling_replay_manifest(manifest_path)


def test_replay_manifest_rejects_source_capture_hardlink_alias(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path / "route")
    capture_path = tmp_path / "frozen-capture.jsonl"
    JsonlTargetSchedulingCaptureTape(capture_path).append_from_cycle(
        _event(1),
        _published_cycle(route),
        sleeve="long",
    )
    output_root = tmp_path / "offline-replay"
    run_offline_target_scheduling_replay(capture_path, output_root=output_root)
    (tmp_path / "capture-alias.jsonl").hardlink_to(capture_path)

    with pytest.raises(
        ValueError,
        match="non-empty regular file|singly linked|hard-linked",
    ):
        load_offline_target_scheduling_replay_manifest(
            output_root / "replay_manifest.json"
        )
