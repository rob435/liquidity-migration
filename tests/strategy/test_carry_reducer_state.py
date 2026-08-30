from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import liquidity_migration.strategy.carry_state as state_module
from liquidity_migration.rules.carry_contract import PriorState
from liquidity_migration.strategy.carry_state import (
    CarryCycleState,
    persist_carry_exit_state,
)
from liquidity_migration.strategy.carry_runtime import in_scope_presettlement_events
from liquidity_migration.strategy.presettlement_events import CarryPresettlementEvent


DECISION_MS = 1_754_999_940_000


def _presettlement_event() -> CarryPresettlementEvent:
    return CarryPresettlementEvent(
        environment="demo",
        source_profile="carry_hold_v7",
        source_config_id="carry_cfg",
        decision_ts_ms=DECISION_MS,
        fired_ts_ms=DECISION_MS + 1_000,
        settlement_ts_ms=DECISION_MS + 900_000,
        symbol="AUSDT",
        running_rate=0.0,
        mark_px=10.0,
        carry_side="long",
        carry_qty=1.0,
        carry_avg_entry_px=9.0,
    )


def test_durable_events_require_exact_deployment_identity() -> None:
    event = _presettlement_event()
    events = (
        event,
        dataclasses.replace(event, source_profile="carry_hold_v6"),
        dataclasses.replace(event, source_config_id="other_cfg"),
        dataclasses.replace(event, environment="mainnet"),
    )

    assert in_scope_presettlement_events(
        events,
        environment="demo",
        source_profile="carry_hold_v7",
        source_config_id="carry_cfg",
    ) == (event,)


def test_presettlement_event_rejects_boolean_schema() -> None:
    payload = _presettlement_event().to_dict()
    payload["schema_version"] = True

    with pytest.raises(ValueError, match="unsupported pre-settlement event schema"):
        CarryPresettlementEvent.from_dict(payload)


def test_legacy_anchor_and_exit_files_import_into_one_atomic_checkpoint(tmp_path: Path) -> None:
    checkpoint_path = (tmp_path / ".cache" / "carry_sizing_anchors.json").resolve()
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "anchors": {str(DECISION_MS): 1_000.0},
            }
        )
    )
    exit_path = (tmp_path / "carry_early_exits.json").resolve()
    persist_carry_exit_state(exit_path, {"AUSDT": DECISION_MS})
    state = CarryCycleState()
    state.bind_sizing_anchors(checkpoint_path)

    imported = state.reducer_prior(exit_state_path=exit_path)
    assert imported == PriorState(
        ((DECISION_MS, 1_000.0),),
        (("AUSDT", DECISION_MS),),
    )
    state.persist_reducer_state(exit_state_path=exit_path, state=imported)

    payload = json.loads(checkpoint_path.read_text())
    assert payload == {
        "anchors": {str(DECISION_MS): 1_000.0},
        "fired": {"AUSDT": DECISION_MS},
        "schema_version": 2,
    }
    restarted = CarryCycleState()
    restarted.bind_sizing_anchors(checkpoint_path)
    assert restarted.reducer_prior(exit_state_path=exit_path) == imported


def test_checkpoint_replace_failure_exposes_neither_half_of_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_path = (tmp_path / ".cache" / "carry_sizing_anchors.json").resolve()
    exit_path = (tmp_path / "carry_early_exits.json").resolve()
    state = CarryCycleState()
    state.bind_sizing_anchors(checkpoint_path)
    initial = PriorState(((DECISION_MS, 1_000.0),), ())
    state.persist_reducer_state(exit_state_path=exit_path, state=initial)
    before = checkpoint_path.read_bytes()
    real_replace = state_module.durable_atomic_replace

    def fail_checkpoint(path: Path, data: bytes, *, label: str) -> None:
        if label == "CARRY reducer checkpoint":
            raise OSError("checkpoint disk full")
        real_replace(path, data, label=label)

    monkeypatch.setattr(state_module, "durable_atomic_replace", fail_checkpoint)
    with pytest.raises(OSError, match="checkpoint disk full"):
        state.persist_reducer_state(
            exit_state_path=exit_path,
            state=PriorState(
                ((DECISION_MS, 1_000.0), (DECISION_MS + 86_400_000, 1_300.0)),
                (("AUSDT", DECISION_MS),),
            ),
        )

    assert checkpoint_path.read_bytes() == before
    assert state.reducer_prior(exit_state_path=exit_path) == initial


def test_canonical_checkpoint_is_not_gated_by_legacy_mirror_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_path = (tmp_path / ".cache" / "carry_sizing_anchors.json").resolve()
    exit_path = (tmp_path / "carry_early_exits.json").resolve()
    state = CarryCycleState()
    state.bind_sizing_anchors(checkpoint_path)
    initial = PriorState(((DECISION_MS, 1_000.0),), ())
    state.persist_reducer_state(exit_state_path=exit_path, state=initial)

    def fail_legacy_mirror(path: Path, fired: object) -> None:
        raise OSError("legacy mirror unavailable")

    monkeypatch.setattr(state_module, "persist_carry_exit_state", fail_legacy_mirror)
    transitioned = PriorState(
        ((DECISION_MS, 1_000.0),),
        (("AUSDT", DECISION_MS),),
    )
    state.persist_reducer_state(exit_state_path=exit_path, state=transitioned)

    payload = json.loads(checkpoint_path.read_text())
    assert payload["fired"] == {"AUSDT": DECISION_MS}
    assert state.reducer_prior(exit_state_path=exit_path) == transitioned
