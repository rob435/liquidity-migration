from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path

import polars as pl
import pytest
import scripts.research.build_trade_diagnostics as diagnostic_script

from liquidity_migration.account.account_kernel import (
    AccountEvent,
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    read_account_journal,
)
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.market_capture import (
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
    capture_record_id,
)
from liquidity_migration.research.execution.trade_diagnostics import (
    EXECUTION_DIAGNOSTIC_SCHEMA,
    TradeDiagnosticError,
    build_execution_diagnostics,
    build_trade_diagnostic_manifest,
    load_post_fill_markouts,
    load_required_book_contexts,
)


def _context(*, sequence_gap: bool = False) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "kind": "book_context",
        "context_kind": "account_service_decision",
        "reference_key": "batch-1",
        "symbol": "BUSDT",
        "local_receive_ts_ns": 1_050_000_000,
        "book_local_receive_ts_ns": 1_000_000_000,
        "exchange_system_ts_ns": 905_000_000,
        "exchange_engine_ts_ns": 900_000_000,
        "update_id": 100,
        "cross_sequence": 1_000,
        "sequence_gap": sequence_gap,
        "sequence_gap_reason": "test_gap" if sequence_gap else "",
        "bids": [[99.9, 2.0], [99.8, 4.0]],
        "asks": [[100.1, 1.0], [100.2, 4.0]],
    }
    record["record_id"] = capture_record_id(record)
    return record


def _persist_context(capture_root: Path) -> dict[str, object]:
    recorder = SequenceAwareMarketRecorder(
        capture_root,
        config=MarketCaptureConfig(
            depth=50,
            segment_max_bytes=1_000_000,
            fsync_every_records=1,
            min_free_disk_bytes=1,
            persist_raw_market=False,
        ),
        clock=VirtualClock(current_wall_ns=1_050_000_000, current_monotonic_ns=0),
    )
    recorder.on_message(
        {
            "topic": "orderbook.50.BUSDT",
            "type": "snapshot",
            "ts": 905,
            "cts": 900,
            "data": {
                "s": "BUSDT",
                "b": [["99.9", "2"], ["99.8", "4"]],
                "a": [["100.1", "1"], ["100.2", "4"]],
                "u": 100,
                "seq": 1_000,
            },
        },
        local_receive_ts_ns=1_000_000_000,
    )
    captured, _book = recorder.capture_context(
        symbol="BUSDT",
        context_kind="account_service_decision",
        reference_key="batch-1",
    )
    recorder.close()
    return captured


def _journal(
    root: Path,
    *,
    context: dict[str, object],
    market_metadata: dict[str, object] | None = None,
    max_order_qty: float = 0.0,
    late_ack_observation: bool = False,
) -> list:
    clock = VirtualClock(current_wall_ns=1_100_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(root, account_id="diagnostic-account", clock=clock, id_seed="diagnostic")
    market_key = str(context["record_id"])
    result = kernel.submit_targets(
        batch_id="batch-1",
        market_inputs=[
            MarketInputRef(
                input_key=market_key,
                symbol="BUSDT",
                exchange_ts_ns=900_000_000,
                local_receive_ts_ns=1_000_000_000,
                reference_price=100.0,
                bid_price=99.9,
                ask_price=100.1,
                book_sequence=1_000,
                source="test_book",
                metadata=market_metadata or {},
            )
        ],
        targets=[
            DesiredTarget(
                decision_key="d-component-a",
                target_key="long/a/BUSDT",
                sleeve="long",
                strategy_id="long-v1",
                component_id="a",
                symbol="BUSDT",
                signed_qty=0.4,
                reference_price=100.0,
                leverage=2.0,
                metadata={"signal_ts_ms": 1_000},
            ),
            DesiredTarget(
                decision_key="d-component-b",
                target_key="long/b/BUSDT",
                sleeve="long",
                strategy_id="long-v1",
                component_id="b",
                symbol="BUSDT",
                signed_qty=0.6,
                reference_price=100.0,
                leverage=2.0,
                metadata={"signal_ts_ms": 1_000},
            ),
        ],
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 1_050_000_000),
        risk_policy=AccountRiskPolicy(1_000.0, 2_000.0, 1_000.0, 10.0),
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT",
                qty_step=0.1,
                min_qty=0.1,
                min_notional=1.0,
                max_order_qty=max_order_qty,
            )
        },
    )
    if len(result.commands) == 1:
        fill_parts = (
            (0.4, 100.1, 0.004, 1_170_000_000, 1_190_000_000),
            (0.6, 100.2, 0.006, 1_180_000_000, 1_200_000_000),
        )
    else:
        fill_parts = ()
    for command_index, command in enumerate(result.commands, start=1):
        command_offset = 0 if len(result.commands) == 1 else command_index
        if late_ack_observation:
            kernel.record_ack(
                command_id=command.command_id,
                accepted=True,
                venue_order_id=f"venue-{command_index}",
                exchange_ts_ns=1_169_000_000 + command_offset,
                local_ack_ts_ns=1_189_000_000 + command_offset,
                metadata={"inferred_from_execution_id": "synthetic-race"},
            )
        kernel.record_ack(
            command_id=command.command_id,
            accepted=True,
            venue_order_id=f"venue-{command_index}",
            exchange_ts_ns=1_140_000_000 + command_offset,
            local_ack_ts_ns=1_160_000_000 + command_offset,
            metadata={"local_socket_send_ts_ns": 1_150_000_000 + command_offset},
        )
        command_fills = fill_parts or (
            (
                command.qty,
                100.1 + command_index / 100.0,
                command.qty / 100.0,
                1_170_000_000 + command_index,
                1_190_000_000 + command_index,
            ),
        )
        for fill_index, (qty, price, fee, exchange_ts_ns, local_ts_ns) in enumerate(
            command_fills,
            start=1,
        ):
            kernel.record_fill(
                command_id=command.command_id,
                execution_id=f"fill-{command_index}-{fill_index}",
                signed_qty=qty,
                price=price,
                fee_usdt=fee,
                exchange_ts_ns=exchange_ts_ns,
                local_receive_ts_ns=local_ts_ns,
                metadata={
                    "fee_observed": True,
                    "fee_status": "observed_execution_fee",
                    "is_maker": False,
                    "execution_type": "Trade",
                    "fee_currency": "USDT",
                },
            )
    return read_account_journal(root, verify=True)


def _synthetic_markout_evidence(
    events: Sequence[AccountEvent],
) -> tuple[dict[str, dict[str, object]], dict[tuple[str, int], dict[str, object]]]:
    horizons = (1_000_000_000, 15_000_000_000, 60_000_000_000, 300_000_000_000)
    schedules: dict[str, dict[str, object]] = {}
    observations: dict[tuple[str, int], dict[str, object]] = {}
    fills = [
        event
        for event in events
        if event.event_type == AccountEventType.FILL.value
    ]
    for fill in fills:
        payload = fill.payload
        execution_id = str(payload["execution_id"])
        command_id = str(payload["command_id"])
        fill_local_ns = int(payload["local_receive_ts_ns"])
        schedule: dict[str, object] = {
            "schema_version": 1,
            "kind": "post_fill_markout_schedule",
            "symbol": fill.symbol,
            "local_receive_ts_ns": fill_local_ns + 1,
            "execution_id": execution_id,
            "command_id": command_id,
            "signed_qty": payload["signed_qty"],
            "fill_price": payload["price"],
            "fill_exchange_ts_ns": payload["exchange_ts_ns"],
            "fill_local_receive_ts_ns": fill_local_ns,
            "target_horizons_ns": list(horizons),
            "max_lateness_ns": 50_000_000,
            "schedule_status": "accepted",
            "pending_limit": 8_192,
        }
        schedule["record_id"] = capture_record_id(schedule)
        schedules[execution_id] = schedule
        for index, horizon_ns in enumerate(horizons, start=1):
            actual_horizon_ns = horizon_ns + 20_000_000
            record: dict[str, object] = {
                "schema_version": 1,
                "kind": "post_fill_markout",
                "symbol": fill.symbol,
                "local_receive_ts_ns": fill_local_ns + actual_horizon_ns,
                "book_local_receive_ts_ns": fill_local_ns + actual_horizon_ns,
                "exchange_system_ts_ns": 0,
                "exchange_engine_ts_ns": 0,
                "update_id": 200 + index,
                "cross_sequence": 2_000 + index,
                "sequence_gap": False,
                "sequence_gap_reason": "",
                "bids": [[100.2, 2.0]],
                "asks": [[100.4, 2.0]],
                "execution_id": execution_id,
                "command_id": command_id,
                "signed_qty": payload["signed_qty"],
                "fill_price": payload["price"],
                "fill_exchange_ts_ns": payload["exchange_ts_ns"],
                "fill_local_receive_ts_ns": fill_local_ns,
                "target_horizon_ns": horizon_ns,
                "target_local_receive_ts_ns": fill_local_ns + horizon_ns,
                "actual_horizon_ns": actual_horizon_ns,
                "observation_lateness_ns": 20_000_000,
                "max_lateness_ns": 50_000_000,
                "markout_status": "observed_healthy",
                "missing_reason": "",
                "markout_midpoint": 100.3,
            }
            record["record_id"] = capture_record_id(record)
            observations[(execution_id, horizon_ns)] = record
    return schedules, observations


def test_command_diagnostics_use_unique_decision_grain_and_exact_book(tmp_path: Path) -> None:
    context = _context()
    events = _journal(tmp_path / "account", context=context)

    frame = build_execution_diagnostics(events, {str(context["record_id"]): context})

    assert frame.schema == EXECUTION_DIAGNOSTIC_SCHEMA
    assert frame.height == 1
    row = frame.to_dicts()[0]
    assert row["contributing_target_count"] == 2
    assert row["decision_unit_count"] == 1
    assert row["decision_unit_keys_json"] == '["long|BUSDT|1000"]'
    assert row["context_status"] == "healthy"
    assert row["decision_mid"] == pytest.approx(100.0)
    assert row["quoted_spread_bps"] == pytest.approx(20.0)
    assert row["top_imbalance"] == pytest.approx(1.0 / 3.0)
    assert row["microprice"] == pytest.approx((100.1 * 2.0 + 99.9) / 3.0)
    assert row["book_walk_vwap"] == pytest.approx(100.1)
    assert row["book_walk_shortfall_bps"] == pytest.approx(10.0)
    assert row["fill_vwap"] == pytest.approx(100.16)
    assert row["arrival_shortfall_bps"] == pytest.approx(16.0)
    assert row["effective_spread_bps"] == pytest.approx(32.0)
    assert row["book_walk_residual_bps"] == pytest.approx(6.0)
    assert row["fee_usdt"] == pytest.approx(0.01)
    assert row["fee_coverage"] == "observed_complete"
    assert row["fee_bps"] == pytest.approx(10_000.0 * 0.01 / 100.16)
    assert row["all_in_arrival_bps"] == pytest.approx(16.0 + 10_000.0 * 0.01 / 100.16)
    assert row["maker_fill_fraction"] == 0.0
    assert row["maker_coverage"] == "complete"
    assert row["fill_ratio"] == 1.0
    assert row["decision_book_age_ns"] == 100_000_000
    assert row["decision_to_send_ns"] == 50_000_000
    assert row["local_request_rtt_ns"] == 10_000_000
    assert row["send_to_first_local_fill_ns"] == 40_000_000
    assert row["exchange_ack_to_first_fill_ns"] == 30_000_000
    assert row["exchange_fill_span_ns"] == 10_000_000
    assert row["feed_delivery_plus_clock_offset_ns"] == 100_000_000
    assert row["fill_delivery_plus_clock_offset_ns"] == 20_000_000
    assert row["markout_status"] == "incomplete"
    assert row["markout_1s_status"] == "not_registered"
    assert row["markout_1s_coverage"] == 0.0

    manifest = build_trade_diagnostic_manifest(
        events=events,
        book_contexts={str(context["record_id"]): context},
        diagnostics=frame,
        code_commit="a" * 40,
        account_root="/account",
        capture_root="/capture",
        context_lookup={"required_contexts": 1, "missing_context_ids": []},
    )
    assert manifest["output"]["rows"] == 1
    assert manifest["output"]["grain_counts"] == {
        "commands": 1,
        "executions": 2,
        "unique_decisions": 1,
        "waves": 1,
    }
    assert len(manifest["manifest_payload_sha256"]) == 64


def test_manifest_does_not_count_chunked_commands_as_independent_decisions(
    tmp_path: Path,
) -> None:
    context = _context()
    events = _journal(
        tmp_path / "account",
        context=context,
        max_order_qty=0.6,
    )
    frame = build_execution_diagnostics(events, {str(context["record_id"]): context})

    manifest = build_trade_diagnostic_manifest(
        events=events,
        book_contexts={str(context["record_id"]): context},
        diagnostics=frame,
        code_commit="a" * 40,
        account_root="/account",
        capture_root="/capture",
        context_lookup={"required_contexts": 1, "missing_context_ids": []},
    )

    assert frame.height == 2
    assert manifest["output"]["grain_counts"] == {
        "commands": 2,
        "executions": 2,
        "unique_decisions": 1,
        "waves": 1,
    }


def test_http_ack_observation_owns_request_timing_after_inferred_ack(
    tmp_path: Path,
) -> None:
    context = _context()
    events = _journal(
        tmp_path / "account",
        context=context,
        late_ack_observation=True,
    )

    row = build_execution_diagnostics(
        events,
        {str(context["record_id"]): context},
    ).to_dicts()[0]

    assert row["local_socket_send_ts_ns"] == 1_150_000_000
    assert row["local_ack_ts_ns"] == 1_160_000_000
    assert row["exchange_ack_ts_ns"] == 1_140_000_000
    assert row["local_request_rtt_ns"] == 10_000_000
    assert row["exchange_ack_to_first_fill_ns"] == 30_000_000


def test_sequence_gap_is_explicit_and_not_used_for_tca(tmp_path: Path) -> None:
    healthy = _context()
    events = _journal(tmp_path / "account", context=healthy)
    gapped = _context(sequence_gap=True)

    row = build_execution_diagnostics(events, {str(healthy["record_id"]): gapped}).to_dicts()[0]

    assert row["context_status"] == "sequence_gap"
    assert row["decision_mid"] is None
    assert row["arrival_shortfall_bps"] is None
    assert "decision_book:sequence_gap" in row["missing_reasons_json"]


def test_markouts_are_fill_weighted_at_execution_coverage_grain(
    tmp_path: Path,
) -> None:
    context = _context()
    events = _journal(tmp_path / "account", context=context)
    schedules, observations = _synthetic_markout_evidence(events)

    row = build_execution_diagnostics(
        events,
        {str(context["record_id"]): context},
        markout_schedules=schedules,
        markout_observations=observations,
    ).to_dicts()[0]

    first = 10_000.0 * (100.3 - 100.1) / 100.1
    second = 10_000.0 * (100.3 - 100.2) / 100.2
    expected = 0.4 * first + 0.6 * second
    assert row["markout_status"] == "complete"
    assert row["markout_1s_status"] == "complete"
    assert row["markout_1s_coverage"] == 1.0
    assert row["markout_1s_actual_horizon_ns"] == 1_020_000_000
    assert row["markout_1s_bps"] == pytest.approx(expected)
    assert row["post_fill_adverse_1s_bps"] == pytest.approx(-expected)
    assert len(json.loads(row["markout_1s_source_records_json"])) == 2


def test_markout_observation_must_match_registered_lateness_bound(
    tmp_path: Path,
) -> None:
    context = _context()
    events = _journal(tmp_path / "account", context=context)
    schedules, observations = _synthetic_markout_evidence(events)
    key = next(iter(observations))
    changed = dict(observations[key])
    changed["max_lateness_ns"] = 60_000_000
    changed["record_id"] = capture_record_id(changed)
    observations[key] = changed

    with pytest.raises(TradeDiagnosticError, match="contradicts execution"):
        build_execution_diagnostics(
            events,
            {str(context["record_id"]): context},
            markout_schedules=schedules,
            markout_observations=observations,
        )


def test_missing_markout_cannot_precede_the_registered_lateness_bound(
    tmp_path: Path,
) -> None:
    context = _context()
    events = _journal(tmp_path / "account", context=context)
    schedules, observations = _synthetic_markout_evidence(events)
    key = next(iter(observations))
    changed = dict(observations[key])
    changed["markout_status"] = "missing"
    changed["missing_reason"] = "synthetic_missing"
    changed["markout_midpoint"] = None
    changed["record_id"] = capture_record_id(changed)
    observations[key] = changed

    with pytest.raises(TradeDiagnosticError, match="not explicit"):
        build_execution_diagnostics(
            events,
            {str(context["record_id"]): context},
            markout_schedules=schedules,
            markout_observations=observations,
        )


def test_post_fill_loader_recovers_only_canonical_execution_records(
    tmp_path: Path,
) -> None:
    context = _context()
    events = _journal(tmp_path / "account", context=context)
    schedules, observations = _synthetic_markout_evidence(events)
    capture_root = tmp_path / "capture"
    segment = capture_root / "BUSDT" / "1970-01-01" / "segment-000000.jsonl"
    segment.parent.mkdir(parents=True)
    records = [*schedules.values(), *observations.values()]
    segment.write_bytes(b"".join(canonical_json(record) + b"\n" for record in records))

    loaded_schedules, loaded_observations, report = load_post_fill_markouts(
        capture_root,
        events,
    )

    assert loaded_schedules == schedules
    assert loaded_observations == observations
    assert report["required_executions"] == 2
    assert report["accepted_schedules"] == 2
    assert report["observation_records"] == 8
    assert report["missing_schedule_execution_ids"] == []


def test_context_identity_mismatch_fails_projection(tmp_path: Path) -> None:
    context = _context()
    events = _journal(tmp_path / "account", context=context)
    broken = dict(context)
    broken["symbol"] = "WRONGUSDT"

    with pytest.raises(TradeDiagnosticError, match="invalid record identity"):
        build_execution_diagnostics(events, {str(context["record_id"]): broken})


def test_exact_capture_locator_avoids_root_scan(tmp_path: Path) -> None:
    capture_root = tmp_path / "capture"
    captured = _persist_context(capture_root)
    stored_context = _context()
    stored_context["record_id"] = captured["record_id"]
    # The journal only needs the locator fields for this lookup test; its
    # market identity is the actually persisted context.
    events = _journal(
        tmp_path / "account",
        context=stored_context,
        market_metadata={
            key: captured[key]
            for key in (
                "capture_segment_path",
                "capture_byte_offset",
                "capture_byte_length",
                "capture_record_sha256",
            )
        },
    )

    contexts, report = load_required_book_contexts(capture_root, events)

    assert set(contexts) == {captured["record_id"]}
    assert report["direct_locator_contexts"] == 1
    assert report["legacy_scan_files"] == 0
    assert report["missing_context_ids"] == []
    assert math.isclose(contexts[captured["record_id"]]["bids"][0][0], 99.9)


def test_capture_locator_detects_content_tampering(tmp_path: Path) -> None:
    capture_root = tmp_path / "capture"
    captured = _persist_context(capture_root)
    events = _journal(
        tmp_path / "account",
        context=captured,
        market_metadata={
            key: captured[key]
            for key in (
                "capture_segment_path",
                "capture_byte_offset",
                "capture_byte_length",
                "capture_record_sha256",
            )
        },
    )
    segment = capture_root / str(captured["capture_segment_path"])
    raw = bytearray(segment.read_bytes())
    raw[int(captured["capture_byte_offset"])] ^= 1
    segment.write_bytes(raw)

    with pytest.raises(TradeDiagnosticError, match="locator hash mismatch"):
        load_required_book_contexts(capture_root, events)


def test_builder_publishes_only_atomic_manifest_and_command_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_root = tmp_path / "capture"
    account_root = tmp_path / "account"
    captured = _persist_context(capture_root)
    _journal(
        account_root,
        context=captured,
        market_metadata={
            key: captured[key]
            for key in (
                "capture_segment_path",
                "capture_byte_offset",
                "capture_byte_length",
                "capture_record_sha256",
            )
        },
    )
    monkeypatch.setattr(
        diagnostic_script,
        "_git",
        lambda *args: " M synthetic-fixture" if args[0] == "status" else "a" * 40,
    )
    output = tmp_path / "diagnostic-run"

    assert diagnostic_script.main(
        [
            "--account-root",
            str(account_root),
            "--capture-root",
            str(capture_root),
            "--out",
            str(output),
            "--allow-dirty",
        ]
    ) == 0

    assert sorted(path.name for path in output.iterdir()) == [
        "execution_tca.parquet",
        "manifest.json",
    ]
    frame = pl.read_parquet(output / "execution_tca.parquet")
    manifest = json.loads((output / "manifest.json").read_text())
    assert frame.height == 1
    assert manifest["source"]["git_dirty"] is True
    assert manifest["output"]["rows"] == 1
    assert manifest["files"]["execution_tca.parquet"]["bytes"] > 0
    assert manifest["source"]["markout_lookup"]["required_executions"] == 2
    assert len(
        manifest["source"]["markout_lookup"][
            "missing_schedule_execution_ids"
        ]
    ) == 2
    assert len(manifest["source"]["markout_evidence_sha256"]) == 64

    with pytest.raises(FileExistsError, match="already exists"):
        diagnostic_script.main(
            [
                "--account-root",
                str(account_root),
                "--capture-root",
                str(capture_root),
                "--out",
                str(output),
                "--allow-dirty",
            ]
        )
