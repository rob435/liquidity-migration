from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.rules.engine_targets import (
    EngineTarget,
    publish_target_book,
    render_target_book,
    write_target_book,
)
from liquidity_migration.strategy.strategy_event_clock import StrategyEvent
from liquidity_migration.strategy.target_book_evidence import (
    JsonlTargetBookCaptureTape,
    PublishedTargetCyclePayload,
)


def _event(sequence: int = 1) -> StrategyEvent:
    return StrategyEvent(
        event_ts_ns=1_000 + sequence,
        ingest_ts_ns=2_000 + sequence,
        source="test",
        source_sequence=sequence,
        kind="timer",
        payload={"execution_environment": "demo", "strategy_profile": "carry_v1"},
    )


def _write_book(path, notional: float = -50.0) -> None:
    write_target_book(
        path,
        render_target_book(
            source="carry_v1",
            decision_ts_ms=1_000,
            valid_until_ms=2_000,
            targets=[EngineTarget("BTCUSDT", notional, 0.35, 2.0)],
        ),
    )


def test_capture_binds_event_to_the_exact_durable_book(tmp_path) -> None:
    book = tmp_path / "book.json"
    tape_path = tmp_path / "captures.jsonl"
    _write_book(book)
    payload = PublishedTargetCyclePayload({"book_written": True}, target_book_path=book)

    capture = JsonlTargetBookCaptureTape(tape_path).append_from_cycle(
        _event(), payload, sleeve="carry"
    )

    assert capture.decision_keys == ("carry_v1/BTCUSDT",)
    row = json.loads(tape_path.read_text(encoding="utf-8"))
    assert row["capture"]["event_id"] == _event().event_id
    assert row["capture"]["target_book_sha256"] == payload.target_book_sha256


def test_capture_refuses_a_book_changed_after_cycle_completion(tmp_path) -> None:
    book = tmp_path / "book.json"
    _write_book(book)
    payload = PublishedTargetCyclePayload({}, target_book_path=book)
    _write_book(book, notional=-75.0)

    with pytest.raises(ValueError, match="changed"):
        JsonlTargetBookCaptureTape(tmp_path / "captures.jsonl").append_from_cycle(
            _event(), payload, sleeve="carry"
        )


def test_capture_chain_reloads_and_rejects_duplicate_events(tmp_path) -> None:
    book = tmp_path / "book.json"
    tape_path = tmp_path / "captures.jsonl"
    _write_book(book)
    payload = PublishedTargetCyclePayload({}, target_book_path=book)
    JsonlTargetBookCaptureTape(tape_path).append_from_cycle(_event(), payload, sleeve="carry")

    reloaded = JsonlTargetBookCaptureTape(tape_path)
    with pytest.raises(ValueError, match="duplicate"):
        reloaded.append_from_cycle(_event(), payload, sleeve="carry")

    reloaded.append_from_cycle(_event(2), payload, sleeve="carry")
    assert len(tape_path.read_bytes().splitlines()) == 2


def test_capture_chain_rejects_a_partial_tail(tmp_path) -> None:
    path = tmp_path / "captures.jsonl"
    path.write_bytes(b'{"partial":true}')
    with pytest.raises(ValueError, match="partial line"):
        JsonlTargetBookCaptureTape(path)


def test_capture_points_to_replayable_immutable_book_bytes(tmp_path) -> None:
    book = tmp_path / "book.json"
    published = publish_target_book(
        book,
        render_target_book(
            source="carry_v1",
            decision_ts_ms=1_000,
            valid_until_ms=2_000,
            targets=[EngineTarget("BTCUSDT", -50.0, 0.35, 2.0)],
        ),
    )
    payload = PublishedTargetCyclePayload(
        {},
        target_book_path=book,
        target_book_object_path=published.object_path,
    )
    tape_path = tmp_path / "captures.jsonl"
    JsonlTargetBookCaptureTape(tape_path).append_from_cycle(_event(), payload, sleeve="carry")
    row = json.loads(tape_path.read_text(encoding="utf-8"))

    object_path = row["capture"]["target_book_object"]
    assert Path(object_path).read_bytes() == book.read_bytes()
