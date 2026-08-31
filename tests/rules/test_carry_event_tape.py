from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.rules.carry_event_tape import (
    CarryEventTapeError,
    CarryPresettlementEvent,
    decode_carry_presettlement_events,
    load_carry_presettlement_events,
)
from liquidity_migration.rules.exodus_models import carry_presettlement_event_id


def _carry_event_tape() -> tuple[CarryPresettlementEvent, bytes]:
    event = CarryPresettlementEvent(
        environment="demo",
        source_profile="carry_hold_v7_live_v1",
        source_config_id="lane2_carry_hold_v7",
        decision_ts_ms=1_800_000_000_000,
        fired_ts_ms=1_800_000_300_000,
        settlement_ts_ms=1_800_000_900_000,
        symbol="AUSDT",
        running_rate=-0.0001,
        mark_px=10.0,
        carry_side="long",
        carry_qty=2.0,
        carry_avg_entry_px=9.5,
    )
    payload = {
        "schema_version": 1,
        "event_id": carry_presettlement_event_id(
            environment=event.environment,
            source_config_id=event.source_config_id,
            decision_ts_ms=event.decision_ts_ms,
            settlement_ts_ms=event.settlement_ts_ms,
            symbol=event.symbol,
        ),
        "environment": event.environment,
        "source_profile": event.source_profile,
        "source_config_id": event.source_config_id,
        "decision_ts_ms": event.decision_ts_ms,
        "fired_ts_ms": event.fired_ts_ms,
        "settlement_ts_ms": event.settlement_ts_ms,
        "symbol": event.symbol,
        "running_rate": event.running_rate,
        "mark_px": event.mark_px,
        "carry_side": event.carry_side,
        "carry_qty": event.carry_qty,
        "carry_avg_entry_px": event.carry_avg_entry_px,
    }
    strategy_event = {
        "event_ts_ns": event.fired_ts_ms * 1_000_000,
        "ingest_ts_ns": event.fired_ts_ms * 1_000_000 + 1,
        "source": "carry_hold:demo",
        "source_sequence": 0,
        "kind": "presettlement_exit",
        "payload": payload,
    }
    identity = {
        key: strategy_event[key]
        for key in (
            "event_ts_ns",
            "kind",
            "payload",
            "source",
            "source_sequence",
        )
    }
    strategy_event["event_id"] = "strategy-event-" + hashlib.sha256(
        canonical_json(identity)
    ).hexdigest()
    genesis = hashlib.sha256(
        b"liquidity-migration-strategy-event-tape-v1"
    ).hexdigest()
    tape_hash = hashlib.sha256(
        genesis.encode("ascii") + canonical_json({"event": strategy_event})
    ).hexdigest()
    row = {
        "schema_version": 1,
        "prior_tape_hash": genesis,
        "tape_hash": tape_hash,
        "event": strategy_event,
    }
    return event, canonical_json(row) + b"\n"


def test_event_tape_verifies_hash_chain_and_empty_file(tmp_path: Path) -> None:
    event, tape = _carry_event_tape()

    assert decode_carry_presettlement_events(tape) == (event,)
    with pytest.raises(CarryEventTapeError, match="unterminated"):
        decode_carry_presettlement_events(tape.rstrip(b"\n"))
    empty = tmp_path / "events.jsonl"
    empty.write_bytes(b"")
    assert load_carry_presettlement_events(empty) == ()


def test_event_tape_rejects_a_modified_payload() -> None:
    _, tape = _carry_event_tape()
    changed = tape.replace(b'"running_rate":-0.0001', b'"running_rate":-0.0002')

    with pytest.raises(CarryEventTapeError, match="event id is invalid"):
        decode_carry_presettlement_events(changed)


def test_event_requires_complete_position_fields() -> None:
    with pytest.raises(CarryEventTapeError, match="holding is incomplete"):
        CarryPresettlementEvent(
            environment="demo",
            source_profile="carry_hold_v7_live_v1",
            source_config_id="lane2_carry_hold_v7",
            decision_ts_ms=1_800_000_000_000,
            fired_ts_ms=1_800_000_300_000,
            settlement_ts_ms=1_800_000_900_000,
            symbol="AUSDT",
            running_rate=-0.0001,
            mark_px=10.0,
            carry_side="long",
            carry_qty=None,
            carry_avg_entry_px=9.5,
        )
