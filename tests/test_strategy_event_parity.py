from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration import account_cutover_authority
from liquidity_migration import strategy_event_parity as parity_module
from liquidity_migration import strategy_target_replay as target_replay_module
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.strategy_event_clock import JsonlStrategyEventTape, StrategyEvent
from liquidity_migration.strategy_event_outcome import (
    JsonlStrategyEventDecisionTape,
    load_strategy_event_decision_tape,
)
from liquidity_migration.strategy_event_parity import (
    ENVIRONMENTS,
    build_strategy_event_parity_receipt,
    load_strategy_event_parity_receipt,
    verify_strategy_event_parity_receipt,
    write_strategy_event_parity_receipt,
)
from liquidity_migration.strategy_target_replay import (
    TargetSchedulingCaptureEvent,
    run_offline_target_scheduling_replay,
)


def _write_bound_replay_fixture(
    tmp_path: Path,
) -> tuple[dict[str, Path], dict[str, Path], dict[str, Path], Path, dict[str, Any]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_event = StrategyEvent(
        event_ts_ns=1_000,
        ingest_ts_ns=1_010,
        source="long:demo",
        source_sequence=1,
        kind="timer",
        payload={
            "execution_environment": "demo",
            "strategy_profile": "LongV11aDivWeekendVol",
        },
    )
    captured = TargetSchedulingCaptureEvent(
        source_event=source_event,
        source_environment="demo",
        sleeve="long",
        strategy_profile="LongV11aDivWeekendVol",
        requests=(),
        decision_keys=(),
    )
    prior_hash = target_replay_module._CAPTURE_GENESIS_HASH  # noqa: SLF001
    capture_hash = target_replay_module._capture_hash(prior_hash, captured)  # noqa: SLF001
    capture_path = tmp_path / "canonical-target-capture.jsonl"
    capture_path.write_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "kind": "account_target_scheduling_capture",
                "prior_capture_hash": prior_hash,
                "capture_hash": capture_hash,
                "capture_event": captured.to_dict(),
            }
        )
        + b"\n"
    )
    capture_path.chmod(0o600)
    output_root = tmp_path / "offline-replay"
    manifest = run_offline_target_scheduling_replay(capture_path, output_root=output_root)
    event_tapes = {
        environment: output_root / environment / "strategy_event_tape.jsonl"
        for environment in ENVIRONMENTS
    }
    decision_tapes = {
        environment: output_root / environment / "strategy_event_decision_tape.jsonl"
        for environment in ENVIRONMENTS
    }
    replay_inputs = {
        environment: output_root / environment / "replay_input.jsonl"
        for environment in ENVIRONMENTS
    }
    return (
        event_tapes,
        decision_tapes,
        replay_inputs,
        output_root / "replay_manifest.json",
        manifest,
    )


def _sources() -> dict[str, dict[str, str]]:
    return {environment: {f"long:{environment}": "long:replay"} for environment in ENVIRONMENTS}


def _write_fixture(
    tmp_path: Path,
    *,
    decision_override: dict[str, list[str]] | None = None,
    omit_field: str | None = None,
    different_input_environment: str | None = None,
    ingest_offset_by_environment: dict[str, int] | None = None,
) -> tuple[dict[str, Path], dict[str, Path], dict[str, Path]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tapes: dict[str, Path] = {}
    decisions_by_environment: dict[str, Path] = {}
    inputs: dict[str, Path] = {}
    for environment in ENVIRONMENTS:
        input_path = tmp_path / f"{environment}-input.jsonl"
        content = b'{"bar":1}\n{"bar":2}\n'
        if environment == different_input_environment:
            content = b'{"bar":1}\n{"bar":999}\n'
        input_path.write_bytes(content)
        inputs[environment] = input_path
        input_hash = hashlib.sha256(content).hexdigest()

        tape_path = tmp_path / f"{environment}-events.jsonl"
        tape = JsonlStrategyEventTape(tape_path)
        decision_path = tmp_path / f"{environment}-decisions.jsonl"
        decision_tape = JsonlStrategyEventDecisionTape(decision_path)
        for sequence, (timestamp, kind) in enumerate(((1_000, "market_boundary"), (2_000, "timer")), start=1):
            decisions = [f"long-target/strategy/{sequence}/entry/component"]
            if decision_override and environment in decision_override and sequence == 2:
                decisions = decision_override[environment]
            payload: dict[str, Any] = {
                "execution_environment": environment,
                "replay_input_sha256": input_hash,
                "market_input": {"bar": sequence, "symbol": "BTCUSDT"},
                "strategy_profile": "LongV11aDivWeekendVol",
            }
            if omit_field is not None:
                payload.pop(omit_field, None)
            event = StrategyEvent(
                event_ts_ns=timestamp,
                ingest_ts_ns=timestamp + (ingest_offset_by_environment or {}).get(environment, 10),
                source=f"long:{environment}",
                source_sequence=sequence,
                kind=kind,
                payload=payload,
            )
            tape.append(event)
            decision_tape.append(event.event_id, decisions)
        tapes[environment] = tape_path
        decisions_by_environment[environment] = decision_path
    return tapes, decisions_by_environment, inputs


def test_receipt_reproduces_exact_normalized_event_and_decision_parity(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path)

    receipt = build_strategy_event_parity_receipt(
        tapes,
        decision_tapes=decision_tapes,
        replay_inputs=inputs,
        source_normalizations=_sources(),
    )

    assert receipt["strategy_event_replay_gate_passed"] is True
    assert receipt["report"]["canonical_event_identities_identical"] is True
    assert receipt["report"]["decision_keys_identical"] is True
    assert receipt["report"]["normalized_decision_chains_identical"] is True
    assert receipt["comparison_policy"]["numeric_tolerance_applied"] is False
    assert receipt["replay_provenance"] == {
        "deployment_valid": False,
        "replay_manifest": None,
        "canonical_source_capture": None,
    }
    assert len({receipt["sources"][environment]["event_tape"]["raw_chain_hash"] for environment in ENVIRONMENTS}) == 3
    assert (
        len({receipt["sources"][environment]["event_tape"]["normalized_chain_hash"] for environment in ENVIRONMENTS})
        == 1
    )
    assert (
        len({receipt["sources"][environment]["decision_tape"]["normalized_chain_hash"] for environment in ENVIRONMENTS})
        == 1
    )

    output = tmp_path / "event-parity.json"
    write_strategy_event_parity_receipt(output, receipt)
    assert load_strategy_event_parity_receipt(output) == receipt


def test_receipt_writer_refuses_to_replace_preserved_evidence(tmp_path: Path) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path / "fixture")
    receipt = build_strategy_event_parity_receipt(
        tapes,
        decision_tapes=decision_tapes,
        replay_inputs=inputs,
        source_normalizations=_sources(),
    )
    output = tmp_path / "event-parity.json"
    output.write_bytes(b"preserved failed attempt\n")
    output.chmod(0o600)

    with pytest.raises(FileExistsError):
        write_strategy_event_parity_receipt(output, receipt)

    assert output.read_bytes() == b"preserved failed attempt\n"


def test_builder_rechecks_event_and_decision_tapes_after_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path)
    original = parity_module.load_strategy_event_decision_tape_bytes
    calls = 0

    def mutate_after_demo_parse(data: bytes) -> tuple[Any, str]:
        nonlocal calls
        result = original(data)
        calls += 1
        if calls == len(ENVIRONMENTS):
            tapes["demo"].write_bytes(tapes["demo"].read_bytes() + b"\n")
        return result

    monkeypatch.setattr(
        parity_module,
        "load_strategy_event_decision_tape_bytes",
        mutate_after_demo_parse,
    )

    with pytest.raises(ValueError, match="demo event tape changed"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
        )


def test_demo_replay_manifest_makes_parity_deploy_valid_and_authority_eligible(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs, replay_manifest, manifest = _write_bound_replay_fixture(
        tmp_path
    )
    receipt = build_strategy_event_parity_receipt(
        tapes,
        decision_tapes=decision_tapes,
        replay_inputs=inputs,
        source_normalizations=_sources(),
        replay_manifest=replay_manifest,
    )
    provenance = receipt["replay_provenance"]
    assert provenance["deployment_valid"] is True
    assert provenance["canonical_source_capture"] == manifest["source_capture"]
    assert provenance["replay_manifest"] == {
        "path": str(replay_manifest),
        "size_bytes": replay_manifest.stat().st_size,
        "sha256": hashlib.sha256(replay_manifest.read_bytes()).hexdigest(),
        "schema_version": 2,
        "artifact_sha256": manifest["artifact_sha256"],
        "created_ts_ns": manifest["created_ts_ns"],
    }
    output = tmp_path / "event-parity.json"
    write_strategy_event_parity_receipt(output, receipt)
    assert load_strategy_event_parity_receipt(output) == receipt
    check = account_cutover_authority._machine_validate_evidence(
        role="event_clock_comparison",
        path=output,
        now_ns=receipt["created_ts_ns"] + 1,
    )
    assert check["validator"] == "strategy_event_replay_parity_v3"
    assert check["event_counts"] == {
        "historical": 1,
        "paper": 1,
        "demo": 1,
    }
    assert check["decision_outcome_counts"] == check["event_counts"]


def test_decision_mismatch_produces_a_failed_receipt(tmp_path: Path) -> None:
    tapes, decision_tapes, inputs = _write_fixture(
        tmp_path,
        decision_override={"demo": ["different-decision"]},
    )

    receipt = build_strategy_event_parity_receipt(
        tapes,
        decision_tapes=decision_tapes,
        replay_inputs=inputs,
        source_normalizations=_sources(),
    )

    assert receipt["strategy_event_replay_gate_passed"] is False
    assert receipt["report"]["decision_keys_identical"] is False
    assert receipt["report"]["payloads_identical"] is True
    assert receipt["report"]["canonical_event_identities_identical"] is True
    assert receipt["report"]["normalized_decision_chains_identical"] is False


def test_missing_explicit_input_identity_fails_closed(tmp_path: Path) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path, omit_field="replay_input_sha256")

    with pytest.raises(ValueError, match="replay input hash"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
        )


def test_post_callback_decisions_cannot_be_embedded_in_input_tape(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path)
    rows = [json.loads(line) for line in tapes["demo"].read_text().splitlines()]
    # Rebuild a valid raw event tape with the forbidden field so the failure is
    # the causal boundary, not a stale raw event hash.
    tapes["demo"].unlink()
    decision_tapes["demo"].unlink()
    event_tape = JsonlStrategyEventTape(tapes["demo"])
    decision_tape = JsonlStrategyEventDecisionTape(decision_tapes["demo"])
    for row in rows:
        raw = row["event"]
        payload = dict(raw["payload"])
        payload["decision_keys"] = []
        event = StrategyEvent(
            event_ts_ns=int(raw["event_ts_ns"]),
            ingest_ts_ns=int(raw["ingest_ts_ns"]),
            source=str(raw["source"]),
            source_sequence=int(raw["source_sequence"]),
            kind=str(raw["kind"]),
            payload=payload,
        )
        event_tape.append(event)
        decision_tape.append(event.event_id, [])

    with pytest.raises(ValueError, match="embeds post-callback decision_keys"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
        )


def test_missing_or_misaligned_decision_outcome_fails_closed(tmp_path: Path) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path)
    decision_tapes["demo"].write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
        )

    tapes, decision_tapes, inputs = _write_fixture(tmp_path / "misaligned")
    decision_tapes["demo"].unlink()
    recorder = JsonlStrategyEventDecisionTape(decision_tapes["demo"])
    paper_events = JsonlStrategyEventTape(tapes["paper"]).prior_events
    for event in paper_events:
        recorder.append(event.event_id, [])
    with pytest.raises(ValueError, match="does not align"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
        )


def test_ingest_arrival_telemetry_does_not_change_normalized_parity(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs = _write_fixture(
        tmp_path,
        ingest_offset_by_environment={"historical": 1, "paper": 10, "demo": 99},
    )
    receipt = build_strategy_event_parity_receipt(
        tapes,
        decision_tapes=decision_tapes,
        replay_inputs=inputs,
        source_normalizations=_sources(),
    )

    assert receipt["strategy_event_replay_gate_passed"] is True
    assert receipt["normalization"]["ingest_ts_ns"].startswith("bound_in_raw")


def test_companion_decision_tape_round_trips_and_rejects_duplicate_or_tamper(
    tmp_path: Path,
) -> None:
    event = StrategyEvent(1_000, 1_050, "long:demo", 1, "timer", {})
    path = tmp_path / "decisions.jsonl"
    tape = JsonlStrategyEventDecisionTape(path)
    tape.append(event.event_id, ["decision-a", "decision-b"])

    outcomes, tape_hash = load_strategy_event_decision_tape(path)
    assert outcomes == tape.prior_outcomes
    assert tape_hash == tape.tape_hash
    with pytest.raises(ValueError, match="duplicate"):
        tape.append(event.event_id, [])

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["outcome"]["decision_keys"] = ["changed"]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_strategy_event_decision_tape(path)


def test_mismatched_replay_input_bytes_are_rejected(tmp_path: Path) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path, different_input_environment="demo")

    with pytest.raises(ValueError, match="replay input artifacts differ"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
        )


def test_every_raw_source_requires_one_explicit_noncollapsing_map(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path)
    missing = _sources()
    missing["demo"] = {}
    with pytest.raises(ValueError, match="source normalization is empty"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=missing,
        )

    unused = _sources()
    unused["demo"]["unused:demo"] = "unused:replay"
    unused["paper"]["unused:paper"] = "unused:replay"
    unused["historical"]["unused:historical"] = "unused:replay"
    with pytest.raises(ValueError, match="unused raw sources"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=unused,
        )


def test_duplicate_source_sequence_is_rejected_even_with_distinct_event_ids(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path)
    tapes["demo"].unlink()
    decision_tapes["demo"].unlink()
    event_tape = JsonlStrategyEventTape(tapes["demo"])
    decision_tape = JsonlStrategyEventDecisionTape(decision_tapes["demo"])
    input_hash = hashlib.sha256(inputs["demo"].read_bytes()).hexdigest()
    for timestamp in (1_000, 2_000):
        event = StrategyEvent(
            timestamp,
            timestamp + 5,
            "long:demo",
            1,
            "timer",
            {
                "execution_environment": "demo",
                "replay_input_sha256": input_hash,
                "market_input": {"timestamp": timestamp},
            },
        )
        event_tape.append(event)
        decision_tape.append(event.event_id, [])

    with pytest.raises(ValueError, match="duplicate or backward source sequence"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
        )


def test_empty_corrupt_backward_and_duplicate_tapes_are_rejected(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path)

    tapes["demo"].write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
        )

    tapes, decision_tapes, inputs = _write_fixture(tmp_path / "corrupt")
    rows = [json.loads(line) for line in tapes["demo"].read_text().splitlines()]
    rows[0]["event"]["payload"]["market_input"]["bar"] = 999
    tapes["demo"].write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="id|hash"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
        )

    for failure in ("backward", "duplicate"):
        root = tmp_path / failure
        root.mkdir(parents=True, exist_ok=True)
        tapes, decision_tapes, inputs = _write_fixture(root)
        demo_tape = tapes["demo"]
        demo_tape.unlink()
        recorder = JsonlStrategyEventTape(demo_tape)
        base_payload = {
            "execution_environment": "demo",
            "replay_input_sha256": hashlib.sha256(inputs["demo"].read_bytes()).hexdigest(),
        }
        first = StrategyEvent(
            2_000,
            2_010,
            "long:demo",
            1,
            "timer",
            base_payload,
        )
        recorder.append(first)
        recorder.append(
            first
            if failure == "duplicate"
            else StrategyEvent(
                1_000,
                1_010,
                "long:demo",
                2,
                "timer",
                base_payload,
            )
        )
        with pytest.raises(ValueError, match=failure):
            build_strategy_event_parity_receipt(
                tapes,
                decision_tapes=decision_tapes,
                replay_inputs=inputs,
                source_normalizations=_sources(),
            )


def test_receipt_tampering_or_bound_source_change_is_rejected(tmp_path: Path) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path)
    receipt = build_strategy_event_parity_receipt(
        tapes,
        decision_tapes=decision_tapes,
        replay_inputs=inputs,
        source_normalizations=_sources(),
    )

    changed = json.loads(json.dumps(receipt))
    changed["report"]["decision_keys_identical"] = False
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_strategy_event_parity_receipt(changed)

    output = tmp_path / "receipt.json"
    write_strategy_event_parity_receipt(output, receipt)
    inputs["demo"].write_text('{"changed":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="replay input artifacts differ|does not reproduce"):
        load_strategy_event_parity_receipt(output)


def test_bound_parity_rejects_sources_outside_exact_replay_manifest(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs, replay_manifest, _ = _write_bound_replay_fixture(
        tmp_path / "bound"
    )
    unrelated_tapes, _, _ = _write_fixture(tmp_path / "unrelated")
    tapes["demo"] = unrelated_tapes["demo"]

    with pytest.raises(ValueError, match="does not match the bound target replay manifest"):
        build_strategy_event_parity_receipt(
            tapes,
            decision_tapes=decision_tapes,
            replay_inputs=inputs,
            source_normalizations=_sources(),
            replay_manifest=replay_manifest,
        )


def test_bound_receipt_reopens_manifest_and_its_scheduled_target_tapes(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs, replay_manifest, _ = _write_bound_replay_fixture(
        tmp_path
    )
    receipt = build_strategy_event_parity_receipt(
        tapes,
        decision_tapes=decision_tapes,
        replay_inputs=inputs,
        source_normalizations=_sources(),
        replay_manifest=replay_manifest,
    )
    output = tmp_path / "event-parity.json"
    write_strategy_event_parity_receipt(output, receipt)
    schedule_path = tmp_path / "offline-replay" / "demo" / "scheduled_target_requests.jsonl"
    schedule_path.write_bytes(schedule_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="changed after replay publication"):
        load_strategy_event_parity_receipt(output)


def test_event_parity_receipt_loader_rejects_nonprivate_or_hardlinked_receipt(
    tmp_path: Path,
) -> None:
    tapes, decision_tapes, inputs = _write_fixture(tmp_path / "fixture")
    receipt = build_strategy_event_parity_receipt(
        tapes,
        decision_tapes=decision_tapes,
        replay_inputs=inputs,
        source_normalizations=_sources(),
    )
    output = tmp_path / "event-parity.json"
    write_strategy_event_parity_receipt(output, receipt)
    output.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        load_strategy_event_parity_receipt(output)

    output.chmod(0o600)
    (tmp_path / "event-parity-alias.json").hardlink_to(output)
    with pytest.raises(ValueError, match="singly linked"):
        load_strategy_event_parity_receipt(output)
