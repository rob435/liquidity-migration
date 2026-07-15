from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import liquidity_migration.execution_twin_calibration as calibration_module
from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.execution_twin_calibration import (
    CalibrationRequirements,
    calibrate_execution_twin,
    execution_twin_config_from_calibration,
    require_decision_grade_calibration_requirements,
    verify_calibration_receipt,
)
from liquidity_migration.market_capture import capture_record_id


ACCOUNT_ID = "bybit-demo-unified"
OFFSET_NS = 100_000_000


def _capture_row(**values: object) -> dict[str, object]:
    row: dict[str, object] = {
        "schema_version": 1,
        "kind": "orderbook_snapshot",
        "symbol": "BUSDT",
        "local_receive_ts_ns": 1_102_000_000,
        "exchange_system_ts_ns": 1_000_000_000,
        "exchange_engine_ts_ns": 1_000_000_000,
        "update_id": 1,
        "cross_sequence": 1,
        "sequence_gap": False,
        "trade_ids": None,
        "bids": [[9.99, 10.0]],
        "asks": [[10.01, 10.0]],
    }
    row.update(values)
    row["record_id"] = capture_record_id(row)
    return row


def _build_demo_tapes(
    tmp_path: Path,
    *,
    requested_qty: float = 1.0,
    fill_qty: float = 1.0,
    fill_quantities: tuple[float, ...] | None = None,
    fill_exchange_spacing_ns: int = 1_000_000,
    order_count: int = 1,
    record_fill: bool = True,
    market_reference_price: float = 10.0,
    idempotent_existing_order: bool = False,
    inferred_ack_first: bool = False,
) -> tuple[Path, Path]:
    capture_root = tmp_path / "capture"
    segment = capture_root / "2026-01-01" / "BUSDT" / "segment-000000.jsonl"
    segment.parent.mkdir(parents=True)
    if order_count < 1:
        raise ValueError("order_count must be positive")
    raw = _capture_row()
    contexts = [
        _capture_row(
            kind="book_context",
            context_kind="account_service_decision",
            reference_key=f"batch-{order_index + 1}",
            local_receive_ts_ns=1_103_000_000 + order_index,
            book_local_receive_ts_ns=1_102_000_000,
            update_id=order_index + 1,
            cross_sequence=order_index + 1,
        )
        for order_index in range(order_count)
    ]
    segment.write_bytes(
        b"".join(canonical_json(row) + b"\n" for row in (raw, *contexts))
    )

    account_root = tmp_path / "account"
    clock = VirtualClock(current_wall_ns=1_100_000_000)
    kernel = AccountExecutionKernel(account_root, account_id=ACCOUNT_ID, clock=clock, id_seed="calibration-test")
    quantities = fill_quantities if fill_quantities is not None else (fill_qty,)
    if record_fill and not quantities:
        raise ValueError("record_fill requires at least one fill quantity")
    total_fill_qty = sum(quantities) if record_fill else 0.0
    terminal = (
        "filled"
        if record_fill and abs(total_fill_qty - requested_qty) <= 1e-12
        else "partially_filled_cancelled"
        if record_fill
        else "rejected"
    )
    terminal_index = max(len(quantities) - 1, 0) if record_fill else 0
    for order_index, context in enumerate(contexts, start=1):
        result = kernel.submit_targets(
            batch_id=f"batch-{order_index}",
            market_inputs=[
                MarketInputRef(
                    input_key=str(context["record_id"]),
                    symbol="BUSDT",
                    exchange_ts_ns=1_000_000_000,
                    local_receive_ts_ns=1_102_000_000,
                    reference_price=market_reference_price,
                    bid_price=9.99,
                    ask_price=10.01,
                    book_sequence=1,
                    source="bybit_raw_l2",
                )
            ],
            targets=[
                DesiredTarget(
                    decision_key=f"decision-{order_index}",
                    target_key=f"long/main-{order_index}/BUSDT",
                    sleeve="long",
                    strategy_id="long-v1",
                    component_id=f"main-{order_index}",
                    symbol="BUSDT",
                    signed_qty=requested_qty,
                    reference_price=10.0,
                    leverage=2.0,
                )
            ],
            risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 1_099_000_000),
            risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
            instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, max_order_qty=100.0, max_leverage=10.0)},
        )
        assert result.accepted and len(result.commands) == 1
        command = result.commands[0]
        if inferred_ack_first:
            kernel.record_ack(
                command_id=command.command_id,
                accepted=True,
                venue_order_id=f"venue-{order_index}",
                exchange_ts_ns=1_004_000_000,
                local_ack_ts_ns=1_105_000_000,
                metadata={"inferred_from_execution_id": f"execution-{order_index}-1"},
            )
        kernel.record_ack(
            command_id=command.command_id,
            accepted=True,
            venue_order_id=f"venue-{order_index}",
            exchange_ts_ns=1_003_000_000,
            local_ack_ts_ns=1_106_000_000,
            metadata={
                "local_socket_send_ts_ns": 1_101_000_000,
                "idempotent_existing_order": idempotent_existing_order,
            },
        )
        if record_fill:
            for fill_index, quantity in enumerate(quantities):
                kernel.record_fill(
                    command_id=command.command_id,
                    execution_id=f"execution-{order_index}-{fill_index + 1}",
                    signed_qty=quantity,
                    price=10.02,
                    fee_usdt=quantity * 10.02 * 0.00055,
                    exchange_ts_ns=(
                        1_004_000_000 + fill_index * fill_exchange_spacing_ns
                    ),
                    local_receive_ts_ns=(
                        1_107_000_000 + fill_index * fill_exchange_spacing_ns
                    ),
                )
        kernel.record_order_status(
            command_id=command.command_id,
            status=terminal,
            cumulative_filled_qty=total_fill_qty,
            exchange_ts_ns=1_005_000_000 + terminal_index * 1_000_000,
            local_receive_ts_ns=1_108_000_000 + terminal_index * 1_000_000,
        )
        kernel.record_close(
            close_key=f"close-{order_index}",
            symbol="BUSDT",
            reason="calibration_fixture",
            venue_flat=False,
            command_id=command.command_id,
            exchange_ts_ns=1_008_000_000 + terminal_index * 1_000_000,
            local_receive_ts_ns=1_111_000_000 + terminal_index * 1_000_000,
        )
        kernel.record_pnl(
            pnl_key=f"pnl-{order_index}",
            close_key=f"close-{order_index}",
            symbol="BUSDT",
            gross_pnl_usdt=1.0,
            fee_usdt=0.1,
            funding_usdt=0.0,
            net_pnl_usdt=0.9,
            exchange_ts_ns=1_009_000_000 + terminal_index * 1_000_000,
            local_receive_ts_ns=1_112_000_000 + terminal_index * 1_000_000,
            source="bybit_closed_pnl",
        )
    return account_root, capture_root


def _requirements() -> CalibrationRequirements:
    return CalibrationRequirements(
        min_feed_samples=1,
        min_target_events=1,
        min_order_commands=1,
        min_request_ack_samples=1,
        min_filled_orders=1,
        min_pnl_events=1,
        min_symbols=1,
        min_context_link_ratio=1.0,
    )


def test_single_fill_sample_is_latency_slippage_smoke_only(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="a" * 64,
        requirements=_requirements(),
    )

    assert receipt["market_order_smoke_gate_passed"] is True
    assert receipt["partial_fill_gate"] == {
        "observed_multi_fill_orders": False,
        "partial_fill_spacing_samples": False,
    }
    assert receipt["partial_fill_calibration_gate_passed"] is False
    assert receipt["execution_twin_gate_passed"] is False
    assert receipt["latency_ns"]["feed_clock_adjusted"]["p50"] == 2_000_000
    assert receipt["latency_ns"]["decision_to_socket"]["p50"] == 1_000_000
    assert receipt["latency_ns"]["order_entry_clock_adjusted"]["p50"] == 2_000_000
    assert receipt["latency_ns"]["order_response_clock_adjusted"]["p50"] == 3_000_000
    assert receipt["latency_ns"]["submit_to_first_fill_clock_adjusted"]["p50"] == 3_000_000
    assert receipt["latency_ns"]["fill_response_clock_adjusted"]["p50"] == 3_000_000
    assert receipt["slippage"]["adverse_bps"]["p50"] == pytest.approx(20.0)
    assert receipt["slippage"]["visible_book_walk_adverse_bps"]["p50"] == pytest.approx(10.0)
    assert receipt["slippage"]["residual_adverse_bps_after_visible_book"]["p50"] == pytest.approx(10.0)
    assert receipt["slippage"]["fee_bps"] == pytest.approx(5.5)
    assert receipt["queue_assumption"]["passive_queue_calibrated"] is False

    legacy = {**receipt, "schema_version": 2}
    with pytest.raises(ValueError, match="unknown execution-twin calibration schema"):
        verify_calibration_receipt(legacy, require_registered_requirements=False)

    with pytest.raises(ValueError, match="calibration gate"):
        execution_twin_config_from_calibration(
            receipt,
            max_decision_age_ns=250_000_000,
            require_registered_requirements=False,
        )

    config = execution_twin_config_from_calibration(
        receipt,
        max_decision_age_ns=250_000_000,
        require_gate=False,
        require_registered_requirements=False,
    )
    assert config.latency.order_entry_ns == 2_000_000
    assert config.latency.submit_to_first_fill_ns == 3_000_000
    assert config.latency.fill_response_ns == 3_000_000
    assert config.latency.fill_spacing_ns == 0
    assert config.allow_partial_fills is False
    assert config.fill_partition_policy == "single_level_full_fill_or_reject"
    assert config.residual_adverse_slippage_bps == pytest.approx(10.0)


def test_one_multifill_is_not_a_repeated_spacing_basis(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(
        tmp_path,
        requested_qty=2.0,
        fill_quantities=(1.0, 1.0),
    )
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="4" * 64,
        requirements=_requirements(),
    )

    assert receipt["market_order_smoke_gate_passed"] is True
    assert receipt["fills"]["multi_fill_orders"] == 1
    assert receipt["latency_ns"]["partial_fill_spacing"]["count"] == 1
    assert receipt["partial_fill_gate"] == {
        "observed_multi_fill_orders": False,
        "partial_fill_spacing_samples": False,
    }
    assert receipt["partial_fill_calibration_gate_passed"] is False
    assert receipt["execution_twin_gate_passed"] is False


def test_three_multifills_and_spacings_unlock_partial_fill_config(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(
        tmp_path,
        requested_qty=2.0,
        fill_quantities=(1.0, 1.0),
        order_count=3,
    )
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="4" * 64,
        requirements=_requirements(),
    )

    assert receipt["market_order_smoke_gate_passed"] is True
    assert receipt["fills"]["observed_partial_fill_orders"] == 3
    assert receipt["partial_fill_gate"] == {
        "observed_multi_fill_orders": True,
        "partial_fill_spacing_samples": True,
    }
    assert receipt["partial_fill_calibration_gate_passed"] is True
    assert receipt["execution_twin_gate_passed"] is True
    assert receipt["latency_ns"]["partial_fill_spacing"]["p50"] == 1_000_000
    assert receipt["fills"]["book_level_partition_calibrated"] is False

    config = execution_twin_config_from_calibration(
        receipt,
        max_decision_age_ns=250_000_000,
        require_registered_requirements=False,
    )
    assert config.latency.fill_spacing_ns == 1_000_000
    assert config.allow_partial_fills is True
    assert config.fill_partition_policy == "book_level"


def test_equal_timestamp_multifill_does_not_invent_spacing(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(
        tmp_path,
        requested_qty=2.0,
        fill_quantities=(1.0, 1.0),
        fill_exchange_spacing_ns=0,
        order_count=3,
    )
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="5" * 64,
        requirements=_requirements(),
    )

    assert receipt["fills"]["multi_fill_orders"] == 3
    assert receipt["fills"]["observed_partial_fill_orders"] == 3
    assert receipt["latency_ns"]["partial_fill_spacing"]["count"] == 0
    assert receipt["latency_ns"]["partial_fill_spacing"]["p50"] is None
    assert receipt["partial_fill_gate"] == {
        "observed_multi_fill_orders": True,
        "partial_fill_spacing_samples": False,
    }
    assert receipt["partial_fill_calibration_gate_passed"] is False
    assert receipt["execution_twin_gate_passed"] is False


def test_partial_fill_frequency_is_observed_not_assumed(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path, requested_qty=2.0, fill_qty=1.0)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="b" * 64,
        requirements=_requirements(),
    )
    assert receipt["market_order_smoke_gate_passed"] is True
    assert receipt["fills"]["incomplete_orders"] == 1
    assert receipt["fills"]["incomplete_order_rate"] == 1.0
    assert receipt["fills"]["observed_partial_fill_orders"] == 1
    assert receipt["partial_fill_gate"] == {
        "observed_multi_fill_orders": False,
        "partial_fill_spacing_samples": False,
    }
    assert receipt["partial_fill_calibration_gate_passed"] is False
    assert receipt["execution_twin_gate_passed"] is False


def test_decision_reference_must_match_captured_book_midpoint(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(
        tmp_path,
        market_reference_price=9.90,
    )
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="f" * 64,
        requirements=_requirements(),
    )

    assert receipt["sample_gate"]["context_link_ratio"] is True
    assert receipt["reference_error_bps"]["p50"] == pytest.approx(100.0)
    assert receipt["sample_gate"]["reference_match_ratio"] is False
    assert receipt["execution_twin_gate_passed"] is False


def test_missing_clock_receipt_preserves_rtt_but_blocks_calibration_gate(
    tmp_path: Path,
) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        requirements=_requirements(),
    )
    assert receipt["latency_ns"]["request_ack_round_trip"]["p50"] == 5_000_000
    assert receipt["latency_ns"]["order_entry_clock_adjusted"]["count"] == 0
    assert receipt["sample_gate"]["clock_offset_receipt"] is False
    assert receipt["execution_twin_gate_passed"] is False
    with pytest.raises(ValueError, match="calibration gate"):
        execution_twin_config_from_calibration(
            receipt,
            max_decision_age_ns=250_000_000,
            require_registered_requirements=False,
        )


def test_idempotent_duplicate_lookup_is_not_a_latency_sample(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(
        tmp_path,
        idempotent_existing_order=True,
    )
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="2" * 64,
        requirements=_requirements(),
    )

    assert receipt["sample_counts"]["accepted_acks"] == 1
    assert receipt["sample_counts"]["request_ack_rtt"] == 0
    assert receipt["sample_gate"]["request_ack_samples"] is False
    assert receipt["execution_twin_gate_passed"] is False


def test_late_http_ack_observation_preserves_request_latency_sample(
    tmp_path: Path,
) -> None:
    account_root, capture_root = _build_demo_tapes(
        tmp_path,
        inferred_ack_first=True,
    )

    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="3" * 64,
        requirements=_requirements(),
    )

    assert receipt["sample_counts"]["accepted_acks"] == 1
    assert receipt["sample_counts"]["request_ack_rtt"] == 1
    assert receipt["latency_ns"]["request_ack_round_trip"]["p50"] == 5_000_000


def test_zero_fill_terminal_order_does_not_satisfy_filled_order_gate(
    tmp_path: Path,
) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path, record_fill=False)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="d" * 64,
        requirements=_requirements(),
    )

    assert receipt["sample_counts"]["terminal_or_fully_filled_orders"] == 1
    assert receipt["sample_counts"]["filled_orders"] == 0
    assert receipt["fills"]["zero_fill_terminal_orders"] == 1
    assert receipt["fills"]["incomplete_orders"] == 0
    assert receipt["fills"]["incomplete_order_rate"] is None
    assert receipt["sample_gate"]["filled_orders"] is False
    assert receipt["sample_gate"]["slippage_samples"] is False
    assert receipt["execution_twin_gate_passed"] is False


def test_implausible_clock_correction_blocks_nonnegative_latency_gate(
    tmp_path: Path,
) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=200_000_000,
        clock_offset_receipt_sha256="e" * 64,
        requirements=_requirements(),
    )

    assert receipt["negative_adjusted_feed_latency_ratio"] == 1.0
    assert receipt["sample_gate"]["nonnegative_adjusted_feed_latency"] is False
    assert receipt["sample_gate"]["nonnegative_adjusted_order_response_latency"] is False
    assert receipt["execution_twin_gate_passed"] is False


def test_calibration_receipt_self_hash_detects_change(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="c" * 64,
        requirements=_requirements(),
    )
    changed = json.loads(json.dumps(receipt))
    changed["sample_counts"]["fill_events"] += 1
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_calibration_receipt(changed)


def test_rehashed_receipt_cannot_override_failed_sample_gate(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        requirements=_requirements(),
    )
    forged = json.loads(json.dumps(receipt))
    forged["execution_twin_gate_passed"] = True
    forged["artifact_sha256"] = hashlib.sha256(canonical_json({**forged, "artifact_sha256": ""})).hexdigest()

    with pytest.raises(ValueError, match="aggregate gate is inconsistent"):
        verify_calibration_receipt(forged)


def test_rehashed_receipt_cannot_hide_an_untimed_filled_order(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="6" * 64,
        requirements=_requirements(),
    )
    forged = json.loads(json.dumps(receipt))
    forged["sample_counts"]["filled_orders"] = 2
    forged["artifact_sha256"] = hashlib.sha256(
        canonical_json({**forged, "artifact_sha256": ""})
    ).hexdigest()

    with pytest.raises(ValueError, match="sample gate does not reproduce"):
        verify_calibration_receipt(forged)


def test_rehashed_receipt_cannot_invent_partial_fill_identifiability(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="7" * 64,
        requirements=_requirements(),
    )
    forged = json.loads(json.dumps(receipt))
    forged["partial_fill_gate"] = {
        "observed_multi_fill_orders": True,
        "partial_fill_spacing_samples": True,
    }
    forged["partial_fill_calibration_gate_passed"] = True
    forged["fills"]["partial_fill_calibrated"] = True
    forged["fills"]["allow_partial_fills"] = True
    forged["execution_twin_gate_passed"] = True
    forged["artifact_sha256"] = hashlib.sha256(
        canonical_json({**forged, "artifact_sha256": ""})
    ).hexdigest()

    with pytest.raises(ValueError, match="partial-fill gate does not reproduce"):
        verify_calibration_receipt(forged)


def test_rehashed_receipt_cannot_weaken_registered_requirements(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="9" * 64,
        requirements=_requirements(),
    )

    with pytest.raises(ValueError, match="weaken registered floors"):
        verify_calibration_receipt(receipt, require_registered_requirements=True)


def test_partial_fill_floor_cannot_be_disabled() -> None:
    with pytest.raises(ValueError, match="min_observed_multi_fill_orders"):
        CalibrationRequirements(
            min_observed_multi_fill_orders=0,
        )
    with pytest.raises(ValueError, match="min_partial_fill_spacing_samples"):
        CalibrationRequirements(
            min_partial_fill_spacing_samples=0,
        )


def test_registered_partial_fill_floor_requires_repeated_samples() -> None:
    assert CalibrationRequirements().min_observed_multi_fill_orders == 3
    assert CalibrationRequirements().min_partial_fill_spacing_samples == 3
    with pytest.raises(ValueError, match="min_observed_multi_fill_orders"):
        require_decision_grade_calibration_requirements(
            CalibrationRequirements(
                min_observed_multi_fill_orders=1,
                min_partial_fill_spacing_samples=1,
            )
        )


def test_duplicate_capture_identity_is_rejected_before_linkage(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    segment = next(capture_root.rglob("segment-*.jsonl"))
    first = segment.read_bytes().splitlines()[0]
    with segment.open("ab") as handle:
        handle.write(first + b"\n")

    with pytest.raises(ValueError, match="duplicate capture record id"):
        calibrate_execution_twin(
            account_root=account_root,
            market_capture_root=capture_root,
            expected_account_id=ACCOUNT_ID,
            observed_ts_ns=2_000_000_000,
            local_minus_exchange_ns=OFFSET_NS,
            clock_offset_receipt_sha256="1" * 64,
            requirements=_requirements(),
        )


def test_calibration_rejects_source_mutation_during_computation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_root, capture_root = _build_demo_tapes(tmp_path)
    segment = next(capture_root.rglob("segment-*.jsonl"))
    original_journal_sha256 = calibration_module._journal_sha256

    def mutate_after_initial_snapshot(
        events: list[calibration_module.AccountEvent],
    ) -> str:
        segment.write_bytes(segment.read_bytes())
        return original_journal_sha256(events)

    monkeypatch.setattr(
        calibration_module,
        "_journal_sha256",
        mutate_after_initial_snapshot,
    )
    with pytest.raises(RuntimeError, match="sources mutated during computation"):
        calibrate_execution_twin(
            account_root=account_root,
            market_capture_root=capture_root,
            expected_account_id=ACCOUNT_ID,
            observed_ts_ns=2_000_000_000,
            local_minus_exchange_ns=OFFSET_NS,
            clock_offset_receipt_sha256="2" * 64,
            requirements=_requirements(),
        )
