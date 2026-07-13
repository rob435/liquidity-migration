from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

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
    record_fill: bool = True,
    market_reference_price: float = 10.0,
    idempotent_existing_order: bool = False,
) -> tuple[Path, Path]:
    capture_root = tmp_path / "capture"
    segment = capture_root / "2026-01-01" / "BUSDT" / "segment-000000.jsonl"
    segment.parent.mkdir(parents=True)
    raw = _capture_row()
    context = _capture_row(
        kind="book_context",
        context_kind="account_service_decision",
        reference_key="batch-1",
        local_receive_ts_ns=1_103_000_000,
        book_local_receive_ts_ns=1_102_000_000,
    )
    segment.write_bytes(canonical_json(raw) + b"\n" + canonical_json(context) + b"\n")

    account_root = tmp_path / "account"
    clock = VirtualClock(current_wall_ns=1_100_000_000)
    kernel = AccountExecutionKernel(
        account_root, account_id=ACCOUNT_ID, clock=clock, id_seed="calibration-test"
    )
    result = kernel.submit_targets(
        batch_id="batch-1",
        market_inputs=[MarketInputRef(
            input_key=str(context["record_id"]),
            symbol="BUSDT",
            exchange_ts_ns=1_000_000_000,
            local_receive_ts_ns=1_102_000_000,
            reference_price=market_reference_price,
            bid_price=9.99,
            ask_price=10.01,
            book_sequence=1,
            source="bybit_raw_l2",
        )],
        targets=[DesiredTarget(
            decision_key="decision-1",
            target_key="long/main/BUSDT",
            sleeve="long",
            strategy_id="long-v1",
            component_id="main",
            symbol="BUSDT",
            signed_qty=requested_qty,
            reference_price=10.0,
            leverage=2.0,
        )],
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 1_099_000_000),
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={
            "BUSDT": InstrumentRules(
                "BUSDT", 0.1, 0.1, 1.0, max_order_qty=100.0, max_leverage=10.0
            )
        },
    )
    assert result.accepted and len(result.commands) == 1
    command = result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-1",
        exchange_ts_ns=1_003_000_000,
        local_ack_ts_ns=1_106_000_000,
        metadata={
            "local_socket_send_ts_ns": 1_101_000_000,
            "idempotent_existing_order": idempotent_existing_order,
        },
    )
    if record_fill:
        kernel.record_fill(
            command_id=command.command_id,
            execution_id="execution-1",
            signed_qty=fill_qty,
            price=10.02,
            fee_usdt=fill_qty * 10.02 * 0.00055,
            exchange_ts_ns=1_004_000_000,
            local_receive_ts_ns=1_107_000_000,
        )
    terminal = (
        "filled"
        if record_fill and fill_qty == requested_qty
        else "partially_filled_cancelled"
        if record_fill
        else "rejected"
    )
    kernel.record_order_status(
        command_id=command.command_id,
        status=terminal,
        cumulative_filled_qty=fill_qty if record_fill else 0.0,
        exchange_ts_ns=1_005_000_000,
        local_receive_ts_ns=1_108_000_000,
    )
    kernel.record_close(
        close_key="close-1",
        symbol="BUSDT",
        reason="calibration_fixture",
        venue_flat=False,
        command_id=command.command_id,
        exchange_ts_ns=1_008_000_000,
        local_receive_ts_ns=1_111_000_000,
    )
    kernel.record_pnl(
        pnl_key="pnl-1",
        close_key="close-1",
        symbol="BUSDT",
        gross_pnl_usdt=1.0,
        fee_usdt=0.1,
        funding_usdt=0.0,
        net_pnl_usdt=0.9,
        exchange_ts_ns=1_009_000_000,
        local_receive_ts_ns=1_112_000_000,
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


def test_calibration_recovers_latency_fill_slippage_and_fee(tmp_path: Path) -> None:
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

    assert receipt["execution_twin_gate_passed"] is True
    assert receipt["latency_ns"]["feed_clock_adjusted"]["p50"] == 2_000_000
    assert receipt["latency_ns"]["decision_to_socket"]["p50"] == 1_000_000
    assert receipt["latency_ns"]["order_entry_clock_adjusted"]["p50"] == 2_000_000
    assert receipt["latency_ns"]["order_response_clock_adjusted"]["p50"] == 3_000_000
    assert receipt["slippage"]["adverse_bps"]["p50"] == pytest.approx(20.0)
    assert receipt["slippage"]["visible_book_walk_adverse_bps"]["p50"] == pytest.approx(10.0)
    assert receipt["slippage"]["residual_adverse_bps_after_visible_book"]["p50"] == pytest.approx(10.0)
    assert receipt["slippage"]["fee_bps"] == pytest.approx(5.5)
    assert receipt["queue_assumption"]["passive_queue_calibrated"] is False

    config = execution_twin_config_from_calibration(
        receipt,
        max_decision_age_ns=250_000_000,
        require_registered_requirements=False,
    )
    assert config.latency.order_entry_ns == 2_000_000
    assert config.allow_partial_fills is True
    assert config.residual_adverse_slippage_bps == pytest.approx(10.0)


def test_partial_fill_frequency_is_observed_not_assumed(tmp_path: Path) -> None:
    account_root, capture_root = _build_demo_tapes(
        tmp_path, requested_qty=2.0, fill_qty=1.0
    )
    receipt = calibrate_execution_twin(
        account_root=account_root,
        market_capture_root=capture_root,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=2_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="b" * 64,
        requirements=_requirements(),
    )
    assert receipt["fills"]["incomplete_orders"] == 1
    assert receipt["fills"]["incomplete_order_rate"] == 1.0


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
    with pytest.raises(ValueError, match="sample gate"):
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
    forged["artifact_sha256"] = hashlib.sha256(
        canonical_json({**forged, "artifact_sha256": ""})
    ).hexdigest()

    with pytest.raises(ValueError, match="aggregate gate is inconsistent"):
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
