from __future__ import annotations

import hashlib
import json
import math
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

import pytest

from liquidity_migration import clock_offset_series as clock_series_module
from liquidity_migration import execution_twin_drift as drift_module
from liquidity_migration.account_intent_client import (
    AccountTargetPublisher,
    ExitFirstPublication,
    component_target_key,
    requested_target,
)
from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    read_account_journal,
)
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.account_service import SleeveAdapterKind
from liquidity_migration.clock_offset_receipt import (
    CLOCK_OFFSET_ENDPOINT,
    capture_clock_offset,
    write_clock_offset_receipt,
)
from liquidity_migration.clock_offset_series import (
    build_clock_offset_series,
    write_clock_offset_series,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.captured_account_replay import (
    POST_WINDOW_SAFETY_REASON,
    POST_WINDOW_SAFETY_SCOPE,
    POST_WINDOW_SAFETY_STRATEGY_PROFILE,
    build_post_window_safety_manifest,
)
from liquidity_migration.execution_twin_calibration import (
    CalibrationRequirements,
    calibrate_execution_twin,
    write_calibration_receipt,
)
from liquidity_migration.execution_twin_drift import (
    BASELINE_CONFIG_ROLE,
    STRESS_CONFIG_ROLE,
    build_execution_twin_config_artifact,
    build_execution_twin_drift_receipt,
    build_v7_archive_source_map,
    load_execution_twin_drift_receipt,
    load_v7_archive_source_map,
    verify_execution_twin_config_artifact,
    verify_execution_twin_drift_receipt,
    write_execution_twin_config_artifact,
    write_execution_twin_drift_receipt,
    write_v7_archive_source_map,
)
from liquidity_migration.market_capture import capture_record_id
from liquidity_migration.strategy_event_clock import StrategyEvent
from liquidity_migration.strategy_target_replay import (
    JsonlTargetSchedulingCaptureTape,
    PublishedTargetCyclePayload,
)


ACCOUNT_ID = "bybit-demo-unified"
OFFSET_NS = 100_000_000
SYMBOLS = ("AUSDT", "BUSDT", "CUSDT")
HOUR_NS = 3_600_000_000_000
T0_NS = 1_800_000_000_000_000_000
T1_NS = T0_NS + 120 * HOUR_NS
FREEZE_ID = "natural-drift-test"


def test_execution_twin_config_rejects_unregistered_decision_age() -> None:
    with pytest.raises(ValueError, match="registered 250000000 ns"):
        build_execution_twin_config_artifact(
            {},
            role=BASELINE_CONFIG_ROLE,
            max_decision_age_ns=250_000_001,
        )


def _capture_row(**values: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": 1,
        "kind": "orderbook_snapshot",
        "symbol": "AUSDT",
        "local_receive_ts_ns": T0_NS + 102_000_000,
        "exchange_system_ts_ns": T0_NS,
        "exchange_engine_ts_ns": T0_NS,
        "update_id": 1,
        "cross_sequence": 1,
        "sequence_gap": False,
        "trade_ids": None,
        "bids": [[9.99, 100.0]],
        "asks": [[10.01, 100.0]],
    }
    row.update(values)
    row["record_id"] = capture_record_id(row)
    return row


def _context(
    *, symbol: str, batch_id: str, index: int, base_ns: int = T0_NS
) -> dict[str, Any]:
    exchange_ns = base_ns + index * 10_000
    book_local_ns = exchange_ns + OFFSET_NS + 2_000_000
    return _capture_row(
        kind="book_context",
        context_kind="account_service_decision",
        reference_key=batch_id,
        symbol=symbol,
        local_receive_ts_ns=book_local_ns + 1_000_000,
        book_local_receive_ts_ns=book_local_ns,
        exchange_system_ts_ns=exchange_ns,
        exchange_engine_ts_ns=exchange_ns,
        update_id=10_000 + index,
        cross_sequence=20_000 + index,
        sequence_gap_reason="",
    )


def _write_capture(root: Path, rows: list[dict[str, Any]]) -> Path:
    segment = root / "2026-07-14" / "ACCOUNT" / "segment-000000.jsonl"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"".join(canonical_json(row) + b"\n" for row in rows))
    return segment


def _market(context: dict[str, Any]) -> MarketInputRef:
    exchange_ns = int(context["exchange_engine_ts_ns"])
    local_ns = int(context["book_local_receive_ts_ns"])
    return MarketInputRef(
        input_key=str(context["record_id"]),
        symbol=str(context["symbol"]),
        exchange_ts_ns=exchange_ns,
        local_receive_ts_ns=local_ns,
        reference_price=10.0,
        bid_price=9.99,
        ask_price=10.01,
        book_sequence=int(context["cross_sequence"]),
        source="bybit_raw_l2",
        metadata={
            "previous_sequence": None,
            "sequence_gap": False,
            "clock_offset_estimate_ns": local_ns - exchange_ns,
            "capture_record_id": context["record_id"],
            "update_id": context["update_id"],
            "sequence_gap_reason": "",
        },
    )


def _rules() -> dict[str, InstrumentRules]:
    return {
        symbol: InstrumentRules(
            symbol,
            qty_step=0.1,
            min_qty=0.1,
            min_notional=1.0,
            tick_size=0.01,
            max_order_qty=100.0,
            max_leverage=10.0,
            source="bybit_demo_post_only_acceptance_probe",
            environment="demo",
            observed_ts_ns=1_800_000_000_000_000_000,
        )
        for symbol in SYMBOLS
    }


def _build_account(
    root: Path,
    contexts: list[dict[str, Any]],
    *,
    full_calibration: bool,
    natural_fill_price: float = 10.02,
    natural_request_ack_rtt_ns: int = 4_000_000,
    natural_fill_quantities: tuple[float, ...] = (1.0,),
    natural_fill_spacing_ns: int = 1_000_000,
    wall_ts_ns: int = T0_NS + 1_000_000_000,
    target_qty: float | None = None,
    request_content_hashes: dict[str, str] | None = None,
    strict_risk_batches: frozenset[str] = frozenset(),
) -> None:
    kernel = AccountExecutionKernel(
        root,
        account_id=ACCOUNT_ID,
        clock=VirtualClock(current_wall_ns=wall_ts_ns),
        id_seed="execution-twin-drift-test",
    )
    risk = AccountRiskPolicy(100_000.0, 100_000.0, 100_000.0, 100_000.0, 10.0)
    for index, context in enumerate(contexts):
        symbol = str(context["symbol"])
        batch_id = str(context["reference_key"])
        result = kernel.submit_targets(
            batch_id=batch_id,
            market_inputs=[_market(context)],
            targets=[
                    DesiredTarget(
                        decision_key=f"{batch_id}/decision-{index}",
                        target_key=f"long/component-{symbol}/{symbol}",
                        sleeve="long",
                        strategy_id="long-v1",
                        component_id=f"component-{symbol}",
                        symbol=symbol,
                        signed_qty=(
                            target_qty
                            if target_qty is not None
                            else 1.0
                            if not full_calibration or index % 2 == 0
                            else 0.0
                        ),
                    reference_price=10.0,
                    leverage=2.0,
                )
            ],
            risk_snapshot=AccountRiskSnapshot(
                100_000.0, 90_000.0, f"wallet-{index}", 1_099_000_000
            ),
            risk_policy=risk,
            instrument_rules=_rules(),
            command_symbols=(
                {symbol}
                if request_content_hashes is not None
                and batch_id in request_content_hashes
                else None
            ),
            require_strict_risk_reduction=batch_id in strict_risk_batches,
            request_content_hash=(
                request_content_hashes.get(batch_id)
                if request_content_hashes is not None
                else None
            ),
        )
        assert result.accepted and len(result.commands) == 1
        command = result.commands[0]
        socket_send_ns = int(context["book_local_receive_ts_ns"]) + 1_000_000
        kernel.record_ack(
            command_id=command.command_id,
            accepted=True,
            venue_order_id=f"venue-{batch_id}-{index}",
            exchange_ts_ns=int(context["exchange_engine_ts_ns"]) + 3_000_000,
            local_ack_ts_ns=(
                socket_send_ns + 5_000_000
                if full_calibration
                else socket_send_ns + natural_request_ack_rtt_ns
            ),
            metadata={"local_socket_send_ts_ns": socket_send_ns},
        )
        quantities = (
            (0.4, 0.6)
            if full_calibration and index < 3
            else (1.0,)
            if full_calibration
            else natural_fill_quantities
        )
        for fill_index, quantity in enumerate(quantities):
            kernel.record_fill(
                command_id=command.command_id,
                execution_id=f"execution-{batch_id}-{index}-{fill_index}",
                signed_qty=math.copysign(quantity, command.signed_qty),
                price=10.02 if full_calibration else natural_fill_price,
                fee_usdt=quantity
                * (10.02 if full_calibration else natural_fill_price)
                * 0.00055,
                exchange_ts_ns=int(context["exchange_engine_ts_ns"])
                + 4_000_000
                + fill_index
                * (1_000_000 if full_calibration else natural_fill_spacing_ns),
                local_receive_ts_ns=int(context["book_local_receive_ts_ns"])
                + 7_000_000
                + fill_index
                * (1_000_000 if full_calibration else natural_fill_spacing_ns),
            )
        recorded_fill_qty = math.fsum(quantities)
        kernel.record_order_status(
            command_id=command.command_id,
            status=(
                "filled"
                if math.isclose(recorded_fill_qty, command.qty, rel_tol=0.0, abs_tol=1e-12)
                else "partially_filled_cancelled"
            ),
            cumulative_filled_qty=recorded_fill_qty,
            exchange_ts_ns=int(context["exchange_engine_ts_ns"]) + 6_000_000,
            local_receive_ts_ns=int(context["book_local_receive_ts_ns"])
            + 9_000_000,
        )
        if full_calibration:
            kernel.record_close(
                close_key=f"close-{index}",
                symbol=symbol,
                reason="calibration_fixture",
                venue_flat=False,
                command_id=command.command_id,
                exchange_ts_ns=int(context["exchange_engine_ts_ns"]) + 8_000_000,
                local_receive_ts_ns=int(context["book_local_receive_ts_ns"])
                + 11_000_000,
            )
            kernel.record_pnl(
                pnl_key=f"pnl-{index}",
                close_key=f"close-{index}",
                symbol=symbol,
                gross_pnl_usdt=1.0,
                fee_usdt=0.1,
                funding_usdt=0.0,
                net_pnl_usdt=0.9,
                exchange_ts_ns=int(context["exchange_engine_ts_ns"]) + 9_000_000,
                local_receive_ts_ns=int(context["book_local_receive_ts_ns"])
                + 12_000_000,
                source="bybit_closed_pnl",
            )


def _registered_clock(
    path: Path,
    *,
    base_ns: int = T0_NS - 3 * HOUR_NS,
    offset_ns: int = OFFSET_NS,
) -> Path:
    wall_values: list[int] = []
    monotonic_values: list[int] = []
    responses: list[bytes] = []
    for index in range(21):
        start = base_ns + index * 100_000_000
        end = start + 10_000_000
        midpoint = start + 5_000_000
        exchange = midpoint - offset_ns
        wall_values.extend((start, end))
        monotonic_values.extend((index * 20_000_000, index * 20_000_000 + 10_000_000))
        responses.append(
            json.dumps(
                {
                    "retCode": 0,
                    "result": {"timeNano": str(exchange)},
                    "time": exchange // 1_000_000,
                }
            ).encode()
        )

    def next_value(values: Iterator[int]) -> Any:
        return lambda: next(values)

    response_iter = iter(responses)
    receipt = capture_clock_offset(
        request_once=lambda: next(response_iter),
        ntp_synchronized=True,
        endpoint=CLOCK_OFFSET_ENDPOINT,
        sample_count=21,
        selected_count=5,
        interval_seconds=0.0,
        max_rtt_ns=250_000_000,
        max_error_ns=100_000_000,
        wall_time_ns=next_value(iter(wall_values)),
        monotonic_ns=next_value(iter(monotonic_values)),
    )
    return write_clock_offset_receipt(path.resolve(), receipt)


def _demo_rules_file(path: Path) -> Path:
    verified_ns = 1_800_000_000_000_000_000
    rules = _rules()
    evidence: dict[str, Any] = {}
    for symbol in rules:
        order_id = f"order-{symbol}"
        link_id = f"probe-{symbol}"
        evidence[symbol] = {
            "schema_version": 1,
            "kind": "bybit_demo_instrument_rule_probe",
            "environment": "demo",
            "observed_ts_ns": verified_ns,
            "symbol": symbol,
            "probe_price": 10.0,
            "probe_distance_bps": 100.0,
            "lowest_accepted_qty": 0.1,
            "lowest_accepted_notional_usdt": 1.0,
            "highest_rejected_qty": 0.0,
            "highest_rejected_notional_usdt": 0.0,
            "tested_leverage": 10.0,
            "terminal_history_timeout_seconds": 1.0,
            "terminal_history_poll_seconds": 0.0,
            "terminal_history_max_polls": 4,
            "required_terminal_confirmation_polls": 2,
            "attempts": [
                {
                    "step_count": 1,
                    "qty": 0.1,
                    "notional_usdt": 1.0,
                    "accepted": True,
                    "outcome": "verified_cancelled_no_fill",
                    "order_link_id": link_id,
                    "order_id": order_id,
                    "create_ack_source": "bybit_api_demo_order_create",
                    "create_ack_order_id": order_id,
                    "create_ack_order_link_id": link_id,
                    "cancel_ack_source": "bybit_api_demo_order_cancel",
                    "cancel_ack_order_id": order_id,
                    "cancel_ack_order_link_id": link_id,
                    "order_history_source": "bybit_api_demo_order_history",
                    "order_history_query_symbol": symbol,
                    "order_history_query_order_id": order_id,
                    "order_history_query_order_link_id": link_id,
                    "terminal_order_id": order_id,
                    "terminal_order_link_id": link_id,
                    "terminal_status": "Cancelled",
                    "terminal_cum_exec_qty": "0",
                    "terminal_cum_exec_value": "0",
                    "terminal_observed_ts_ns": verified_ns + 1,
                    "terminal_poll_count": 2,
                    "terminal_confirmation_polls": 2,
                    "trade_history_source": "bybit_api_demo_trade_history",
                    "trade_history_query_symbol": symbol,
                    "trade_history_query_order_id": order_id,
                    "trade_history_query_order_link_id": link_id,
                    "trade_history_row_count": 0,
                }
            ],
        }
    payload: dict[str, Any] = {
        "schema_version": 3,
        "kind": "bybit_demo_instrument_rules",
        "status": "passed",
        "environment": "demo",
        "verified_ts_ns": verified_ns,
        "max_probe_notional_usdt": 200.0,
        "probe_distance_bps": 100.0,
        "max_private_requests_per_second": 5,
        "rules": {
            symbol: {
                "qty_step": rule.qty_step,
                "min_qty": rule.min_qty,
                "min_notional": rule.min_notional,
                "tick_size": rule.tick_size,
                "max_order_qty": rule.max_order_qty,
                "max_leverage": rule.max_leverage,
                "source": rule.source,
                "environment": rule.environment,
                "observed_ts_ns": verified_ns,
            }
            for symbol, rule in rules.items()
        },
        "evidence": evidence,
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    path.write_bytes(canonical_json(payload) + b"\n")
    return path.resolve()


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    natural_fill_price: float = 10.02,
    natural_request_ack_rtt_ns: int = 4_000_000,
    natural_fill_quantities: tuple[float, ...] = (1.0,),
    natural_fill_spacing_ns: int = 1_000_000,
    natural_asks: list[list[float]] | None = None,
) -> dict[str, Path]:
    live_account = (tmp_path / "live" / "account").resolve()
    live_capture = (tmp_path / "live" / "capture").resolve()
    contexts = [
        _context(
            symbol=SYMBOLS[(index // 2) % len(SYMBOLS)],
            batch_id=f"v7-{index}",
            index=index,
        )
        for index in range(30)
    ]
    feed = [
        _capture_row(
            symbol=SYMBOLS[index % len(SYMBOLS)],
            local_receive_ts_ns=T0_NS + 102_000_000 + index,
            exchange_system_ts_ns=T0_NS + index,
            exchange_engine_ts_ns=T0_NS + index,
            update_id=index + 1,
            cross_sequence=index + 1,
        )
        for index in range(5_000)
    ]
    _write_capture(live_capture, [*feed, *contexts])
    _build_account(live_account, contexts, full_calibration=True)
    calibration = calibrate_execution_twin(
        account_root=live_account,
        market_capture_root=live_capture,
        expected_account_id=ACCOUNT_ID,
        observed_ts_ns=1_800_000_100_000_000_000,
        local_minus_exchange_ns=OFFSET_NS,
        clock_offset_receipt_sha256="a" * 64,
        requirements=CalibrationRequirements(),
    )
    assert calibration["execution_twin_gate_passed"] is True
    calibration_path = write_calibration_receipt(
        (tmp_path / "v7-calibration.json").resolve(), calibration
    )

    archived_account = (tmp_path / "archive" / "account").resolve()
    archived_capture = (tmp_path / "archive" / "capture").resolve()
    shutil.copytree(live_account, archived_account)
    shutil.copytree(live_capture, archived_capture)
    shutil.rmtree(live_account)
    shutil.rmtree(live_capture)

    route = ensure_account_route(
        account_id=ACCOUNT_ID,
        environment="demo",
        account_root=live_account,
        inbox_root=(tmp_path / "live" / "inbox").resolve(),
    )
    publisher = AccountTargetPublisher(
        route, clock=VirtualClock(current_wall_ns=T0_NS + 1_000_000)
    )
    target_key = component_target_key(
        sleeve=SleeveAdapterKind.LONG,
        strategy_id="long-v1",
        component_id="component-AUSDT",
        symbol="AUSDT",
    )
    natural_intent = requested_target(
        adapter_kind=SleeveAdapterKind.LONG,
        decision_key="natural-0/entry",
        target_key=target_key,
        strategy_id="long-v1",
        component_id="component-AUSDT",
        symbol="AUSDT",
        signed_notional_usdt=10.0,
        leverage=2.0,
        reason="natural drift fixture",
    )
    natural_published = publisher.publish(
        batch_id="natural-0",
        intents=(natural_intent,),
        created_ts_ns=T0_NS + 1_000_000,
    )
    natural_target_capture = (tmp_path / "natural-targets.jsonl").resolve()
    JsonlTargetSchedulingCaptureTape(natural_target_capture).append_from_cycle(
        StrategyEvent(
            event_ts_ns=T0_NS + 1,
            ingest_ts_ns=T0_NS + 2,
            source="long:demo",
            source_sequence=0,
            kind="timer",
            payload={
                "execution_environment": "demo",
                "strategy_profile": "long-natural-v1",
            },
        ),
        PublishedTargetCyclePayload(
            {"fixture": True},
            publication=ExitFirstPublication((), (natural_published,), ()),
            route=route,
        ),
        sleeve="long",
    )

    natural_context = _context(symbol="AUSDT", batch_id="natural-0", index=50_000)
    if natural_asks is not None:
        natural_context["asks"] = natural_asks
        natural_context["record_id"] = capture_record_id(natural_context)
    natural_feed = [
        _capture_row(
            symbol="AUSDT",
            local_receive_ts_ns=T0_NS + 101_000_000 + index,
            exchange_system_ts_ns=T0_NS + index,
            exchange_engine_ts_ns=T0_NS + index,
            update_id=60_000 + index,
            cross_sequence=70_000 + index,
        )
        for index in range(10)
    ]
    _build_account(
        live_account,
        [natural_context],
        full_calibration=False,
        natural_fill_price=natural_fill_price,
        natural_request_ack_rtt_ns=natural_request_ack_rtt_ns,
        natural_fill_quantities=natural_fill_quantities,
        natural_fill_spacing_ns=natural_fill_spacing_ns,
        wall_ts_ns=int(natural_context["book_local_receive_ts_ns"]) + 1_000_000,
        request_content_hashes={"natural-0": natural_published.request.content_hash()},
    )

    safety_created_ns = T1_NS + 1_000_000
    target_digest = hashlib.sha256(target_key.encode("utf-8")).hexdigest()[:16]
    safety_batch = (
        f"natural-safety-flatten/{FREEZE_ID}/{safety_created_ns}/0000/"
        f"{target_digest}"
    )
    safety_intent = requested_target(
        adapter_kind=SleeveAdapterKind.RISK,
        decision_key=f"{safety_batch}/zero",
        target_key=target_key,
        strategy_id="long-v1",
        component_id="component-AUSDT",
        symbol="AUSDT",
        signed_notional_usdt=0.0,
        leverage=2.0,
        reason=POST_WINDOW_SAFETY_REASON,
        metadata={
            "natural_safety_flatten": True,
            "natural_freeze_id": FREEZE_ID,
        },
    )
    safety_published = publisher.publish(
        batch_id=safety_batch,
        intents=(safety_intent,),
        created_ts_ns=safety_created_ns,
    )
    journal_head = read_account_journal(live_account, verify=True)[-1]
    safety_target_capture = (tmp_path / "safety-targets.jsonl").resolve()
    JsonlTargetSchedulingCaptureTape(safety_target_capture).append_from_cycle(
        StrategyEvent(
            event_ts_ns=safety_created_ns,
            ingest_ts_ns=safety_created_ns,
            source="long:demo",
            source_sequence=0,
            kind="timer",
            payload={
                "execution_environment": "demo",
                "strategy_profile": POST_WINDOW_SAFETY_STRATEGY_PROFILE,
                "natural_safety_flatten": True,
                "natural_freeze_id": FREEZE_ID,
                "natural_t1_ns": T1_NS,
                "account_id": ACCOUNT_ID,
                "route_id": route.route_id,
                "journal_sequence": journal_head.sequence,
                "journal_state_hash": journal_head.state_hash,
                "scope": POST_WINDOW_SAFETY_SCOPE,
            },
        ),
        PublishedTargetCyclePayload(
            {"fixture": True},
            publication=ExitFirstPublication((safety_published,), (), ()),
            route=route,
        ),
        sleeve="long",
    )
    safety_manifest = (tmp_path / "safety-manifest.json").resolve()
    build_post_window_safety_manifest(
        target_capture_path=safety_target_capture,
        expected_account_id=ACCOUNT_ID,
        freeze_id=FREEZE_ID,
        t1_ns=T1_NS,
        output_path=safety_manifest,
    )
    safety_context = _context(
        symbol="AUSDT",
        batch_id=safety_batch,
        index=1,
        base_ns=T1_NS + 2_000_000,
    )
    _build_account(
        live_account,
        [safety_context],
        full_calibration=False,
        natural_fill_price=20.0,
        natural_request_ack_rtt_ns=10_000_000_000,
        natural_fill_quantities=(math.fsum(natural_fill_quantities),),
        wall_ts_ns=T1_NS + 3_000_000,
        target_qty=0.0,
        request_content_hashes={safety_batch: safety_published.request.content_hash()},
        strict_risk_batches=frozenset({safety_batch}),
    )
    out_of_window_feed = [
        _capture_row(
            local_receive_ts_ns=T0_NS - 1,
            exchange_system_ts_ns=T0_NS + 1_000_000_000,
            exchange_engine_ts_ns=T0_NS + 1_000_000_000,
            update_id=80_001,
            cross_sequence=80_001,
        ),
        _capture_row(
            local_receive_ts_ns=T1_NS + 1,
            exchange_system_ts_ns=T1_NS - 5_000_000_000,
            exchange_engine_ts_ns=T1_NS - 5_000_000_000,
            update_id=80_002,
            cross_sequence=80_002,
        ),
    ]
    natural_segment = _write_capture(
        live_capture,
        [*natural_feed, *out_of_window_feed, natural_context, safety_context],
    )

    archive_map = build_v7_archive_source_map(
        calibration_file=calibration_path,
        archived_account_root=archived_account,
        archived_market_capture_root=archived_capture,
    )
    archive_map_path = write_v7_archive_source_map(
        (tmp_path / "v7-archive-map.json").resolve(), archive_map
    )
    baseline = build_execution_twin_config_artifact(
        calibration,
        role=BASELINE_CONFIG_ROLE,
        max_decision_age_ns=250_000_000,
    )
    stress = build_execution_twin_config_artifact(
        calibration,
        role=STRESS_CONFIG_ROLE,
        max_decision_age_ns=250_000_000,
    )
    baseline_path = write_execution_twin_config_artifact(
        (tmp_path / "baseline.json").resolve(), baseline
    )
    stress_path = write_execution_twin_config_artifact(
        (tmp_path / "stress.json").resolve(), stress
    )
    rules_path = _demo_rules_file(tmp_path / "demo-rules.json")
    clock_path = _registered_clock(tmp_path / "clock-00.json")

    def artifact_ref(path: Path) -> dict[str, str]:
        payload = json.loads(path.read_bytes())
        return {
            "path": str(path),
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "artifact_sha256": str(payload["artifact_sha256"]),
        }

    freeze_payload: dict[str, Any] = {
        "freeze_id": FREEZE_ID,
        "window": {"t0_ns": T0_NS, "t1_ns": T1_NS},
        "runtime": {
            "account_ids": {
                "demo": ACCOUNT_ID,
                "paper": "bybit-paper-unified",
            },
            "roots": {
                "demo": {
                    "account": str(live_account),
                    "inbox": str((tmp_path / "live" / "inbox").resolve()),
                    "capture": str(live_capture),
                },
                "paper": {
                    "account": str((tmp_path / "paper" / "account").resolve()),
                    "inbox": str((tmp_path / "paper" / "inbox").resolve()),
                    "capture": str((tmp_path / "paper" / "capture").resolve()),
                },
            },
        },
        "v7_training": {
            "calibration": artifact_ref(calibration_path),
            "archive_map": artifact_ref(archive_map_path),
            "baseline_config": artifact_ref(baseline_path),
            "stress_config": artifact_ref(stress_path),
        },
        "population": {"demo_rules": artifact_ref(rules_path)},
        "clock": {"receipt": artifact_ref(clock_path)},
        "test_upstream_source": {},
        "artifact_sha256": "",
    }
    freeze_upstream = (tmp_path / "freeze-upstream.json").resolve()
    freeze_upstream.write_text("frozen upstream source\n", encoding="utf-8")
    freeze_upstream.chmod(0o600)
    freeze_payload["test_upstream_source"] = {
        "path": str(freeze_upstream),
        "sha256": hashlib.sha256(freeze_upstream.read_bytes()).hexdigest(),
    }
    freeze_payload["artifact_sha256"] = hashlib.sha256(
        canonical_json(freeze_payload)
    ).hexdigest()
    freeze_path = (tmp_path / "natural-freeze.json").resolve()
    freeze_path.write_bytes(canonical_json(freeze_payload) + b"\n")
    freeze_path.chmod(0o600)

    def load_test_freeze(path: str | Path) -> dict[str, Any]:
        payload = json.loads(Path(path).read_bytes())
        expected_hash = hashlib.sha256(
            canonical_json({**payload, "artifact_sha256": ""})
        ).hexdigest()
        if payload.get("artifact_sha256") != expected_hash:
            raise ValueError("test freeze self-hash is invalid")
        upstream = payload.get("test_upstream_source")
        if not isinstance(upstream, dict) or hashlib.sha256(
            Path(str(upstream.get("path") or "")).read_bytes()
        ).hexdigest() != upstream.get("sha256"):
            raise ValueError("freeze upstream source changed after creation")
        return payload

    monkeypatch.setattr(
        drift_module,
        "load_natural_cutover_freeze_manifest",
        load_test_freeze,
    )
    monkeypatch.setattr(
        clock_series_module,
        "load_natural_cutover_freeze_manifest",
        load_test_freeze,
    )
    clock_receipts = [clock_path]
    for index in range(1, 22):
        clock_receipts.append(
            _registered_clock(
                tmp_path / f"clock-{index:02d}.json",
                base_ns=T0_NS - 3 * HOUR_NS + index * 6 * HOUR_NS,
                offset_ns=OFFSET_NS + index * 100_000,
            )
        )
    clock_series = build_clock_offset_series(
        freeze_manifest_file=freeze_path,
        receipt_files=clock_receipts,
        created_ts_ns=T1_NS + 4 * HOUR_NS,
    )
    clock_series_path = write_clock_offset_series(
        (tmp_path / "clock-series.json").resolve(), clock_series
    )
    return {
        "calibration": calibration_path,
        "archive_map": archive_map_path,
        "archived_account": archived_account,
        "archived_capture": archived_capture,
        "natural_account": live_account,
        "natural_capture": live_capture,
        "natural_segment": natural_segment,
        "freeze_manifest": freeze_path,
        "freeze_upstream": freeze_upstream,
        "natural_target_capture": natural_target_capture,
        "safety_target_capture": safety_target_capture,
        "safety_manifest": safety_manifest,
        "rules": rules_path,
        "clock": clock_path,
        "clock_series": clock_series_path,
        "baseline": baseline_path,
        "stress": stress_path,
    }


def _build(paths: dict[str, Path]) -> dict[str, Any]:
    return build_execution_twin_drift_receipt(
        calibration_file=paths["calibration"],
        v7_archive_map_file=paths["archive_map"],
        natural_account_root=paths["natural_account"],
        natural_market_capture_root=paths["natural_capture"],
        freeze_manifest_file=paths["freeze_manifest"],
        natural_target_capture_file=paths["natural_target_capture"],
        safety_target_capture_file=paths["safety_target_capture"],
        safety_manifest_file=paths["safety_manifest"],
        demo_rules_file=paths["rules"],
        clock_offset_series_file=paths["clock_series"],
        baseline_config_file=paths["baseline"],
        stress_config_file=paths["stress"],
        expected_account_id=ACCOUNT_ID,
        t0_ns=T0_NS,
        t1_ns=T1_NS,
        observed_ts_ns=1_800_500_000_000_000_000,
    )


def test_source_bound_execution_twin_drift_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    calibration = json.loads(paths["calibration"].read_bytes())
    preserved_calibration = paths["calibration"].read_bytes()
    with pytest.raises(FileExistsError):
        write_calibration_receipt(paths["calibration"], calibration)
    assert paths["calibration"].read_bytes() == preserved_calibration
    archive_map = load_v7_archive_source_map(
        paths["archive_map"], calibration_receipt=calibration
    )
    assert archive_map["original_sources"]["account_root"] == str(
        paths["natural_account"]
    )
    assert archive_map["archived_sources"]["account_root"] == str(
        paths["archived_account"]
    )

    stress = json.loads(paths["stress"].read_bytes())
    forged_stress = {**stress, "latency_quantile": "p50", "artifact_sha256": ""}
    forged_stress["artifact_sha256"] = hashlib.sha256(
        canonical_json(forged_stress)
    ).hexdigest()
    with pytest.raises(ValueError, match="does not reproduce"):
        verify_execution_twin_config_artifact(
            forged_stress,
            calibration_receipt=calibration,
            expected_role=STRESS_CONFIG_ROLE,
        )

    original_capture = paths["natural_segment"].read_bytes()
    extra = _context(symbol="AUSDT", batch_id="orphan", index=99_999)
    paths["natural_segment"].write_bytes(original_capture + canonical_json(extra) + b"\n")
    # An unrelated raw context is outside the exact natural input-key subset;
    # it must not contaminate metrics or create a false linkage failure.
    assert _build(paths)["execution_twin_drift_gate_passed"] is True
    paths["natural_segment"].write_bytes(original_capture)

    receipt = _build(paths)
    assert receipt["execution_twin_drift_gate_passed"] is True
    assert receipt["evidence_result"] == "supports"
    assert receipt["holdout_counts"]["filled_commands"] == 1
    assert receipt["holdout_counts"]["commands"] == 1
    assert receipt["holdout_counts"]["feed_latency_observations"] == 10
    assert receipt["holdout_counts"]["filled_commands_by_sleeve"] == {"long": 1}
    assert receipt["holdout_counts"]["filled_commands_by_symbol"] == {"AUSDT": 1}
    assert receipt["partial_fills"]["evidence_status"] == (
        "insufficient_holdout_spacing_does_not_erase_v7"
    )
    assert receipt["model_scope"]["passive_queue_calibrated"] is False
    assert receipt["natural_scope"]["window"] == {
        "t0_ns": T0_NS,
        "t1_ns": T1_NS,
        "hours": 120,
        "interval": "half_open",
    }
    assert receipt["natural_scope"]["natural_target_capture"]["batch_ids"] == [
        "natural-0"
    ]
    assert len(receipt["natural_scope"]["post_window_safety"]["batch_ids"]) == 1
    assert receipt["natural_scope"]["journal_classification"][
        "safety_command_count"
    ] == 1
    assert receipt["natural_scope"]["post_window_safety"][
        "excluded_from_all_drift_metrics"
    ] is True
    assert receipt["source_files"]["natural_cutover_freeze_manifest"][
        "sha256"
    ] == hashlib.sha256(paths["freeze_manifest"].read_bytes()).hexdigest()
    assert receipt["natural_scope"]["freeze_manifest"]["path"] == str(
        paths["freeze_manifest"]
    )
    series = json.loads(paths["clock_series"].read_bytes())
    assert receipt["clock_correction"]["application"][
        "interpolation_method"
    ] == "piecewise_linear_local_minus_exchange_ns"
    assert receipt["clock_correction"]["application"][
        "uncertainty_is_hard_bound"
    ] is False
    assert receipt["clock_correction"]["application"][
        "max_estimated_uncertainty_ns"
    ] > 0
    assert receipt["gates"]["clock_offset_series_coverage"] is True
    assert receipt["holdout_scope"] == {
        "freeze_id": FREEZE_ID,
        "freeze_artifact_sha256": json.loads(
            paths["freeze_manifest"].read_bytes()
        )["artifact_sha256"],
        "freeze_manifest_file_sha256": hashlib.sha256(
            paths["freeze_manifest"].read_bytes()
        ).hexdigest(),
        "t0_ns": T0_NS,
        "t1_ns": T1_NS,
        "clock_offset_series_artifact_sha256": series["artifact_sha256"],
        "clock_offset_series_file_sha256": hashlib.sha256(
            paths["clock_series"].read_bytes()
        ).hexdigest(),
        "clock_offset_series_sample_count": 22,
        "clock_offset_series_max_observed_gap_ns": 6 * HOUR_NS,
        "clock_offset_series_t0_bracketed": True,
        "clock_offset_series_t1_bracketed": True,
        "natural_batch_ids_sha256": hashlib.sha256(
            json.dumps(
                ["natural-0"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest(),
        "safety_batch_ids_sha256": hashlib.sha256(
            json.dumps(
                receipt["natural_scope"]["post_window_safety"]["batch_ids"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest(),
        "safety_batches_excluded": True,
    }
    assert verify_execution_twin_drift_receipt(receipt) == receipt

    nested = paths["natural_capture"] / "receipt.json"
    with pytest.raises(ValueError, match="nested in source root"):
        write_execution_twin_drift_receipt(nested, receipt)

    output = (tmp_path / "drift.json").resolve()
    write_execution_twin_drift_receipt(output, receipt)
    assert load_execution_twin_drift_receipt(output) == receipt
    assert output.stat().st_mode & 0o777 == 0o600

    v1 = dict(calibration)
    v1["schema_version"] = 1
    v1["artifact_sha256"] = ""
    v1["artifact_sha256"] = hashlib.sha256(canonical_json(v1)).hexdigest()
    v1_path = tmp_path / "v1-calibration.json"
    v1_path.write_bytes(canonical_json(v1) + b"\n")
    with pytest.raises(ValueError, match="schema"):
        build_v7_archive_source_map(
            calibration_file=v1_path.resolve(),
            archived_account_root=paths["archived_account"],
            archived_market_capture_root=paths["archived_capture"],
        )

    # A receipt is invalidated by later source mutation; the verifier never
    # trusts its copied distributions or pass flag.
    paths["natural_segment"].write_bytes(original_capture.replace(b"10.01", b"10.11", 1))
    with pytest.raises(ValueError):
        load_execution_twin_drift_receipt(output)


def test_holdout_failures_are_classified_without_rewriting_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _fixture(
        tmp_path,
        monkeypatch,
        natural_fill_price=10.20,
        natural_request_ack_rtt_ns=-1_000_000,
        natural_fill_quantities=(0.25, 0.25, 0.25, 0.25),
        natural_fill_spacing_ns=2_000_000,
    )

    receipt = _build(paths)

    assert receipt["execution_twin_drift_gate_passed"] is False
    assert receipt["evidence_result"] == "contradicts"
    assert receipt["gates"]["request_ack_nonnegative_ratio"] is False
    assert receipt["gates"]["residual_slippage_envelope"] is False
    assert receipt["gates"]["p95_stress_adverse_coverage"] is False
    assert receipt["gates"]["partial_fill_spacing_envelope"] is False
    assert len(receipt["partial_fills"]["positive_spacing_observations"]) == 3
    assert len(receipt["classifications"]["multifill"]) == 1
    assert receipt["thresholds"]["nonnegative_min_ratio"] == 0.99
    assert receipt["thresholds"]["stress_min_coverage"] == 0.95


def test_terminal_partial_fails_baseline_scope_and_stress_uses_actual_quantity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _fixture(
        tmp_path,
        monkeypatch,
        natural_fill_quantities=(0.5,),
        natural_asks=[[10.01, 0.5], [20.0, 100.0]],
    )

    receipt = _build(paths)

    assert receipt["execution_twin_drift_gate_passed"] is False
    assert receipt["gates"]["model_scope"] is False
    assert receipt["holdout_counts"]["terminal_incomplete_commands"] == 1
    reasons = {row["reason"] for row in receipt["model_scope"]["issues"]}
    assert "baseline_terminal_status_mismatch" in reasons
    assert "baseline_fill_quantity_mismatch" in reasons
    command = receipt["commands"][0]
    assert command["terminal_status"] == "partially_filled_cancelled"
    assert command["filled_qty"] == 0.5
    assert command["baseline_terminal_status"] == "filled"
    assert command["baseline_filled_qty"] == 1.0
    stress = receipt["stress"]["command_observations"][0]
    assert stress["stress_total_fill_qty"] == 1.0
    assert stress["stress_comparison_qty"] == 0.5
    # The second level is intentionally far away; a full-command average would
    # be near 15.  Like-for-like coverage prices only the first 0.5 actually
    # filled by demo.
    assert stress["stress_fill_vwap"] < 11.0


def test_rejects_unregistered_risk_and_command_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    context = _context(
        symbol="AUSDT",
        batch_id="unregistered-extra",
        index=2,
        base_ns=T1_NS + 10_000_000,
    )
    _build_account(
        paths["natural_account"],
        [context],
        full_calibration=False,
        wall_ts_ns=T1_NS + 20_000_000,
        target_qty=1.0,
    )
    with paths["natural_segment"].open("ab") as handle:
        handle.write(canonical_json(context) + b"\n")
    with pytest.raises(ValueError, match="RISK_DECISION scope differs"):
        _build(paths)


def test_rejects_freeze_scope_and_artifact_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    freeze_path = paths["freeze_manifest"]
    original = json.loads(freeze_path.read_bytes())
    cases = (
        "window",
        "account_id",
        "root",
        "calibration",
        "demo_rules",
        "clock",
        "freeze_id",
    )
    for case in cases:
        payload = deepcopy(original)
        if case == "window":
            payload["window"]["t0_ns"] += HOUR_NS
        elif case == "account_id":
            payload["runtime"]["account_ids"]["demo"] = "another-demo-account"
        elif case == "root":
            payload["runtime"]["roots"]["demo"]["capture"] = str(
                (tmp_path / "another-capture").resolve()
            )
        elif case == "calibration":
            payload["v7_training"]["calibration"]["artifact_sha256"] = "f" * 64
        elif case == "demo_rules":
            payload["population"]["demo_rules"]["file_sha256"] = "f" * 64
        elif case == "clock":
            payload["clock"]["receipt"]["artifact_sha256"] = "f" * 64
        else:
            payload["freeze_id"] = "another-freeze"
        payload["artifact_sha256"] = ""
        payload["artifact_sha256"] = hashlib.sha256(
            canonical_json(payload)
        ).hexdigest()
        freeze_path.write_bytes(canonical_json(payload) + b"\n")
        freeze_path.chmod(0o600)
        # The series source-reopens the freeze, so any altered freeze fails
        # before copied drift bindings can be trusted.
        with pytest.raises(ValueError):
            _build(paths)
    freeze_path.write_bytes(canonical_json(original) + b"\n")
    freeze_path.chmod(0o600)


def test_receipt_loader_reopens_freeze_source_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    receipt = _build(paths)
    output = (tmp_path / "drift-freeze-bound.json").resolve()
    write_execution_twin_drift_receipt(output, receipt)

    paths["freeze_upstream"].write_text("mutated\n", encoding="utf-8")
    paths["freeze_upstream"].chmod(0o600)
    with pytest.raises(ValueError, match="freeze upstream source changed"):
        load_execution_twin_drift_receipt(output)
