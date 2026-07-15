from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import liquidity_migration.captured_account_replay as replay_module
from liquidity_migration.account_intent_client import (
    AccountTargetPublisher,
    component_target_key,
    publish_exit_first_target_requests,
    requested_target,
)
from liquidity_migration.account_kernel import (
    GENESIS_HASH,
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
    MarketInputRef,
    read_account_journal,
)
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.account_service import AccountExecutionService, SleeveAdapterKind
from liquidity_migration.account_service_bybit import (
    CapturedBybitMarketProvider,
    CapturedPaperExecutionAdapter,
    VerifiedBybitDemoRulesProvider,
)
from liquidity_migration.captured_account_replay import (
    ACCOUNT_REPLAY_RECEIPT_FILENAME,
    POST_WINDOW_SAFETY_SCOPE,
    POST_WINDOW_SAFETY_STRATEGY_PROFILE,
    build_natural_account_replay_input_manifest,
    build_post_window_safety_manifest,
    load_captured_account_replay_receipt,
    run_captured_account_replay,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.demo_rule_probe import (
    DEMO_RULE_PROBE_EVIDENCE_KIND,
    DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
    DEMO_RULES_KIND,
    DEMO_RULES_SCHEMA_VERSION,
    ORDER_CANCEL_SOURCE,
    ORDER_CREATE_SOURCE,
    ORDER_HISTORY_SOURCE,
    TRADE_HISTORY_SOURCE,
)
from liquidity_migration.execution_adapters import (
    ExecutionTwinConfig,
    LatencyProfile,
    MarketOrderExecutionTwin,
)
from liquidity_migration.execution_twin_calibration import (
    DECISION_GRADE_CALIBRATION_REQUIREMENTS,
)
from liquidity_migration.historical_account_replay import HistoricalAccountReplay
from liquidity_migration.market_capture import MarketCaptureConfig, SequenceAwareMarketRecorder
from liquidity_migration.strategy_event_clock import StrategyEvent
from liquidity_migration.strategy_target_replay import (
    JsonlTargetSchedulingCaptureTape,
    PublishedTargetCyclePayload,
)


ACCOUNT_ID = "captured-demo-account"
WALL_NS = 10_000_000_000
NATURAL_T0_NS = WALL_NS - 100
NATURAL_T1_NS = NATURAL_T0_NS + 120 * 3_600_000_000_000


@dataclass(frozen=True)
class _Fixture:
    source_root: Path
    target_capture: Path
    account_root: Path
    market_root: Path
    rules_file: Path
    policy_file: Path
    calibration_file: Path
    freeze_manifest: Path
    effective_runtime_config_bundle: Path
    safety_target_capture: Path
    safety_manifest: Path
    input_manifest: Path
    batch_id: str
    input_key: str


@pytest.fixture(autouse=True)
def _load_test_freeze_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep replay tests focused; freeze's real loader has its own adversarial suite."""

    monkeypatch.setattr(
        replay_module,
        "load_natural_cutover_freeze_manifest",
        lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
    )

    def load_effective_bundle(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
        resolved = Path(path).resolve(strict=True)
        binding = json.loads(resolved.read_text(encoding="utf-8"))
        binding["path"] = str(resolved)
        binding["file_sha256"] = hashlib.sha256(resolved.read_bytes()).hexdigest()
        return {}, binding

    monkeypatch.setattr(
        replay_module,
        "load_effective_runtime_config_bundle_binding",
        load_effective_bundle,
    )


def _write_test_freeze(
    path: Path,
    *,
    account_root: Path,
    market_root: Path,
    rules_file: Path,
    policy_file: Path,
    calibration_file: Path,
) -> None:
    rules = json.loads(rules_file.read_text(encoding="utf-8"))
    calibration = json.loads(calibration_file.read_text(encoding="utf-8"))
    candidate_file = path.parent / "candidate-universe.json"
    if not candidate_file.exists():
        candidate_file.write_bytes(b'{"symbols":["BTCUSDT"]}\n')
        candidate_file.chmod(0o600)
    payload = {
        "artifact_sha256": hashlib.sha256(b"test-freeze").hexdigest(),
        "freeze_id": "fixture-natural-epoch",
        "repository": {
            "candidate_commit": "a" * 40,
            "origin_main_commit": "b" * 40,
        },
        "window": {"t0_ns": NATURAL_T0_NS, "t1_ns": NATURAL_T1_NS},
        "runtime": {
            "account_ids": {"demo": ACCOUNT_ID, "paper": "fixture-paper"},
            "roots": {
                "demo": {
                    "account": str(account_root.resolve()),
                    "inbox": str((account_root.parent / "demo-inbox").resolve()),
                    "capture": str(market_root.resolve()),
                }
            },
            "routes": {"sha256": hashlib.sha256(b"routes").hexdigest()},
            "risk_policy": {
                "sha256": hashlib.sha256(b"risk-set").hexdigest(),
                "artifacts": {
                    "risk:demo": {
                        "path": str(policy_file.resolve()),
                        "sha256": hashlib.sha256(policy_file.read_bytes()).hexdigest(),
                    }
                },
            },
            "seed": {"sha256": hashlib.sha256(b"seed").hexdigest()},
        },
        "population": {
            "candidate_universe": {
                "path": str(candidate_file.resolve()),
                "file_sha256": hashlib.sha256(candidate_file.read_bytes()).hexdigest(),
                "artifact_sha256": hashlib.sha256(b"candidate-artifact").hexdigest(),
            },
            "demo_rules": {
                "path": str(rules_file.resolve()),
                "file_sha256": hashlib.sha256(rules_file.read_bytes()).hexdigest(),
                "artifact_sha256": rules["artifact_sha256"],
            }
        },
        "v7_training": {
            "calibration": {
                "path": str(calibration_file.resolve()),
                "file_sha256": hashlib.sha256(calibration_file.read_bytes()).hexdigest(),
                "artifact_sha256": calibration["artifact_sha256"],
            }
        },
        "clock": {
            "receipt": {
                "artifact_sha256": hashlib.sha256(b"clock-artifact").hexdigest(),
                "file_sha256": hashlib.sha256(b"clock-file").hexdigest(),
            }
        },
    }
    path.write_bytes(canonical_json(payload) + b"\n")
    path.chmod(0o600)


def _write_test_effective_bundle(
    path: Path,
    *,
    freeze_manifest: Path,
    target_capture: Path,
) -> None:
    freeze = json.loads(freeze_manifest.read_text(encoding="utf-8"))
    candidate = freeze["population"]["candidate_universe"]
    run_config = path.parent / "natural-run-config.json"
    if not run_config.exists():
        run_config.write_bytes(b'{"fixture":"natural-run-config"}\n')
        run_config.chmod(0o600)
    binding = {
        "artifact_sha256": hashlib.sha256(b"effective-config-artifact").hexdigest(),
        "validator": "natural_effective_runtime_config_bundle_v2",
        "created_ts_ns": NATURAL_T0_NS - 1,
        "repository": dict(freeze["repository"]),
        "freeze": {
            "path": str(freeze_manifest.resolve()),
            "file_sha256": hashlib.sha256(freeze_manifest.read_bytes()).hexdigest(),
            "artifact_sha256": freeze["artifact_sha256"],
            "freeze_id": freeze["freeze_id"],
        },
        "natural_run_config": {
            "path": str(run_config.resolve()),
            "file_sha256": hashlib.sha256(run_config.read_bytes()).hexdigest(),
            "artifact_sha256": hashlib.sha256(b"run-config-artifact").hexdigest(),
        },
        "candidate_universe": dict(candidate),
        "window": {
            "t0_ns": freeze["window"]["t0_ns"],
            "t1_ns": freeze["window"]["t1_ns"],
            "interval": "half_open_[t0,t1)",
        },
        "runtime_paths": {
            "target_capture_path": str(target_capture.resolve()),
            "sleeves": {},
        },
        "receipts": {},
        "execution_authorization": "not_granted",
    }
    path.write_bytes(canonical_json(binding) + b"\n")
    path.chmod(0o600)


class _SnapshotProvider:
    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock

    def current(self, *, batch_id: str) -> AccountRiskSnapshot:
        return AccountRiskSnapshot(
            equity_usdt=1_000.0,
            available_margin_usdt=900.0,
            snapshot_key=f"demo-wallet:{batch_id}",
            snapshot_ts_ns=self.clock.wall_time_ns(),
        )


class _AdvancingMarketProvider:
    def __init__(self, provider: CapturedBybitMarketProvider, clock: VirtualClock) -> None:
        self.provider = provider
        self.clock = clock

    def current(
        self,
        symbols: Sequence[str],
        *,
        batch_id: str,
    ) -> Mapping[str, MarketInputRef]:
        self.clock.advance_ns(1)
        return self.provider.current(symbols, batch_id=batch_id)


def _distribution(value: float, count: int) -> dict[str, float | int]:
    return {
        "count": count,
        "min": value,
        "p50": value,
        "p75": value,
        "p95": value,
        "p99": value,
        "max": value,
        "mean": value,
    }


def _journal_sha(events: Sequence[Any]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event.to_dict()))
        digest.update(b"\n")
    return digest.hexdigest()


def _write_calibration(
    path: Path,
    *,
    account_root: Path,
    market_root: Path,
) -> None:
    requirements = asdict(DECISION_GRADE_CALIBRATION_REQUIREMENTS)
    # V7 is a distinct training epoch. The synthetic decision-grade fixture
    # binds stable training identities without pretending the natural holdout
    # journal/capture supplied to the replay are calibration data.
    manifest = [
        {
            "path": "v7-training/segment-000001.jsonl",
            "sha256": hashlib.sha256(b"v7-training-market-capture").hexdigest(),
        }
    ]
    sample_gate = {
        "feed_samples": True,
        "target_events": True,
        "order_commands": True,
        "request_ack_samples": True,
        "order_entry_samples": True,
        "order_response_samples": True,
        "submit_to_first_fill_samples": True,
        "fill_response_samples": True,
        "filled_orders": True,
        "pnl_events": True,
        "symbols": True,
        "context_link_ratio": True,
        "reference_match_ratio": True,
        "slippage_samples": True,
        "clock_offset_receipt": True,
        "nonnegative_adjusted_feed_latency": True,
        "nonnegative_adjusted_order_entry_latency": True,
        "nonnegative_adjusted_order_response_latency": True,
        "nonnegative_adjusted_submit_to_first_fill_latency": True,
        "nonnegative_adjusted_fill_response_latency": True,
    }
    partial_fill_gate = {
        "observed_multi_fill_orders": True,
        "partial_fill_spacing_samples": True,
    }
    receipt: dict[str, Any] = {
        "schema_version": 3,
        "kind": "bybit_demo_market_order_execution_twin_calibration",
        "expected_account_id": ACCOUNT_ID,
        "inputs": {
            "account_root": str((account_root.parent / "archived-v7-account").resolve()),
            "account_journal_sha256": hashlib.sha256(b"v7-training-journal").hexdigest(),
            "account_last_event_hash": hashlib.sha256(b"v7-training-head").hexdigest(),
            "market_capture_root": str((market_root.parent / "archived-v7-market-capture").resolve()),
            "market_capture_manifest": manifest,
            "market_capture_manifest_sha256": hashlib.sha256(canonical_json({"files": manifest})).hexdigest(),
            "local_minus_exchange_ns": 1,
            "clock_offset_receipt_sha256": "a" * 64,
        },
        "requirements": requirements,
        "sample_counts": {
            "journal_events": 100,
            "capture_records": 5_000,
            "feed_latency": requirements["min_feed_samples"],
            "target_events": requirements["min_target_events"],
            "order_commands": requirements["min_order_commands"],
            "accepted_acks": requirements["min_request_ack_samples"],
            "request_ack_rtt": requirements["min_request_ack_samples"],
            "fill_events": requirements["min_filled_orders"],
            "filled_orders": requirements["min_filled_orders"],
            "submit_to_first_fill": requirements["min_filled_orders"],
            "submit_to_first_fill_orders": requirements["min_filled_orders"],
            "fill_response": requirements["min_filled_orders"],
            "fill_response_orders": requirements["min_filled_orders"],
            "terminal_or_fully_filled_orders": requirements["min_filled_orders"],
            "pnl_events": requirements["min_pnl_events"],
            "symbols": requirements["min_symbols"],
            "linked_order_contexts": requirements["min_order_commands"],
            "reference_matches": requirements["min_order_commands"],
            "slippage_orders": requirements["min_filled_orders"],
        },
        "latency_ns": {
            "feed_clock_adjusted": _distribution(1.0, requirements["min_feed_samples"]),
            "decision_to_socket": _distribution(1.0, requirements["min_request_ack_samples"]),
            "request_ack_round_trip": _distribution(2.0, requirements["min_request_ack_samples"]),
            "order_entry_clock_adjusted": _distribution(1.0, requirements["min_request_ack_samples"]),
            "order_response_clock_adjusted": _distribution(1.0, requirements["min_request_ack_samples"]),
            "submit_to_first_fill_clock_adjusted": _distribution(1.0, requirements["min_filled_orders"]),
            "fill_response_clock_adjusted": _distribution(1.0, requirements["min_filled_orders"]),
            "partial_fill_spacing": _distribution(1.0, requirements["min_partial_fill_spacing_samples"]),
        },
        "fills": {
            "multi_fill_orders": requirements["min_observed_multi_fill_orders"],
            "partial_fill_calibrated": True,
            "allow_partial_fills": True,
            "book_level_partition_calibrated": False,
            "calibration_scope": (
                "observed multifill/incomplete occurrence, fill ratio, and positive "
                "within-order venue-timestamp spacing only"
            ),
            "uncalibrated_behavior": "single_level_full_fill_or_reject",
        },
        "slippage": {
            "fee_bps": 5.5,
            "residual_adverse_bps_after_visible_book": _distribution(1.0, requirements["min_filled_orders"]),
        },
        "context_link_ratio": 1.0,
        "reference_match_ratio": 1.0,
        "negative_adjusted_feed_latency_ratio": 0.0,
        "negative_adjusted_order_entry_latency_ratio": 0.0,
        "negative_adjusted_order_response_latency_ratio": 0.0,
        "negative_adjusted_submit_to_first_fill_latency_ratio": 0.0,
        "negative_adjusted_fill_response_latency_ratio": 0.0,
        "sample_gate": sample_gate,
        "market_order_smoke_gate_passed": True,
        "partial_fill_gate": partial_fill_gate,
        "partial_fill_calibration_gate_passed": True,
        "execution_twin_gate_passed": True,
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = hashlib.sha256(canonical_json({**receipt, "artifact_sha256": ""})).hexdigest()
    path.write_bytes(canonical_json(receipt) + b"\n")


def _write_empty_post_window_safety_manifest(
    path: Path,
    *,
    capture_path: Path,
    freeze_id: str,
) -> None:
    build_post_window_safety_manifest(
        target_capture_path=capture_path,
        expected_account_id=ACCOUNT_ID,
        freeze_id=freeze_id,
        t1_ns=NATURAL_T1_NS,
        output_path=path,
    )


def _write_rules(path: Path) -> InstrumentRules:
    rule = InstrumentRules(
        symbol="BTCUSDT",
        qty_step=0.1,
        min_qty=0.1,
        min_notional=1.0,
        tick_size=0.1,
        max_order_qty=100.0,
        max_leverage=10.0,
        source="bybit_demo_post_only_acceptance_probe",
        environment="demo",
        observed_ts_ns=WALL_NS,
    )
    order_id = "probe-order-BTCUSDT-1"
    order_link_id = "lm-demo-rule-BTCUSDT-test-1"
    payload: dict[str, Any] = {
        "schema_version": DEMO_RULES_SCHEMA_VERSION,
        "kind": DEMO_RULES_KIND,
        "status": "passed",
        "environment": "demo",
        "verified_ts_ns": WALL_NS,
        "max_probe_notional_usdt": 200.0,
        "probe_distance_bps": 100.0,
        "max_private_requests_per_second": 5,
        "rules": {"BTCUSDT": asdict(rule)},
        "evidence": {
            "BTCUSDT": {
                "schema_version": DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
                "kind": DEMO_RULE_PROBE_EVIDENCE_KIND,
                "environment": "demo",
                "observed_ts_ns": WALL_NS,
                "symbol": "BTCUSDT",
                "probe_price": 10.0,
                "probe_distance_bps": 100.0,
                "lowest_accepted_qty": 0.1,
                "lowest_accepted_notional_usdt": 1.0,
                "highest_rejected_qty": 0.0,
                "highest_rejected_notional_usdt": 0.0,
                "tested_leverage": 10.0,
                "terminal_history_timeout_seconds": 5.0,
                "terminal_history_poll_seconds": 0.1,
                "terminal_history_max_polls": 50,
                "required_terminal_confirmation_polls": 2,
                "attempts": [
                    {
                        "step_count": 1,
                        "qty": 0.1,
                        "notional_usdt": 1.0,
                        "accepted": True,
                        "outcome": "verified_cancelled_no_fill",
                        "rejection": "",
                        "order_link_id": order_link_id,
                        "order_id": order_id,
                        "create_ack_source": ORDER_CREATE_SOURCE,
                        "create_ack_order_id": order_id,
                        "create_ack_order_link_id": order_link_id,
                        "cancel_ack_source": ORDER_CANCEL_SOURCE,
                        "cancel_ack_order_id": order_id,
                        "cancel_ack_order_link_id": order_link_id,
                        "order_history_source": ORDER_HISTORY_SOURCE,
                        "order_history_query_symbol": "BTCUSDT",
                        "order_history_query_order_id": order_id,
                        "order_history_query_order_link_id": order_link_id,
                        "terminal_order_id": order_id,
                        "terminal_order_link_id": order_link_id,
                        "terminal_status": "Cancelled",
                        "terminal_cum_exec_qty": "0",
                        "terminal_cum_exec_value": "0",
                        "terminal_observed_ts_ns": WALL_NS,
                        "terminal_poll_count": 2,
                        "terminal_confirmation_polls": 2,
                        "trade_history_source": TRADE_HISTORY_SOURCE,
                        "trade_history_query_symbol": "BTCUSDT",
                        "trade_history_query_order_id": order_id,
                        "trade_history_query_order_link_id": order_link_id,
                        "trade_history_row_count": 0,
                    }
                ],
            }
        },
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = hashlib.sha256(canonical_json({**payload, "artifact_sha256": ""})).hexdigest()
    path.write_bytes(canonical_json(payload) + b"\n")
    return rule


def _build_fixture(
    tmp_path: Path,
    *,
    pre_window_request: bool = False,
    stale_market: bool = False,
) -> _Fixture:
    source = tmp_path / "source"
    config = source / "config"
    config.mkdir(parents=True)
    rules_file = config / "demo-rules.json"
    policy_file = config / "risk-policy.json"
    calibration_file = config / "calibration.json"
    rule = _write_rules(rules_file)
    policy = AccountRiskPolicy(1_000.0, 2_000.0, 1_000.0, 1_000.0, 10.0)
    policy_file.write_bytes(canonical_json(asdict(policy)) + b"\n")

    route = ensure_account_route(
        account_id=ACCOUNT_ID,
        environment="demo",
        account_root=source / "demo-account",
        inbox_root=source / "demo-inbox",
    )
    clock = VirtualClock(current_wall_ns=WALL_NS, current_monotonic_ns=777)
    target = requested_target(
        adapter_kind=SleeveAdapterKind.LONG,
        decision_key="captured-entry",
        target_key=component_target_key(
            sleeve=SleeveAdapterKind.LONG,
            strategy_id="captured-v1",
            component_id="main",
            symbol="BTCUSDT",
        ),
        strategy_id="captured-v1",
        component_id="main",
        symbol="BTCUSDT",
        signed_notional_usdt=20.0,
        leverage=2.0,
        reason="captured entry",
    )
    publisher = AccountTargetPublisher(route, clock=clock)
    uncaptured_pre_window = None
    if pre_window_request:
        pre_target = requested_target(
            adapter_kind=SleeveAdapterKind.LONG,
            decision_key="uncaptured-pre-window-entry",
            target_key=component_target_key(
                sleeve=SleeveAdapterKind.LONG,
                strategy_id="captured-v1",
                component_id="pre-window",
                symbol="BTCUSDT",
            ),
            strategy_id="captured-v1",
            component_id="pre-window",
            symbol="BTCUSDT",
            signed_notional_usdt=10.0,
            leverage=2.0,
            reason="uncaptured pre-window entry",
        )
        uncaptured_pre_window = publisher.publish(
            batch_id="uncaptured-pre-window",
            intents=(pre_target,),
            created_ts_ns=WALL_NS - 1,
        ).request
    publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix="captured-cycle",
        exit_intents=(),
        entry_intents=(target,),
        created_ts_ns=WALL_NS,
    )
    assert not publication.errors and len(publication.entry_requests) == 1
    capture_path = source / "target-capture" / "capture.jsonl"
    capture_tape = JsonlTargetSchedulingCaptureTape(capture_path)
    capture_tape.append_from_cycle(
        StrategyEvent(
            event_ts_ns=WALL_NS,
            ingest_ts_ns=WALL_NS + 1,
            source="long:demo",
            source_sequence=1,
            kind="startup",
            payload={
                "execution_environment": "demo",
                "strategy_profile": "LongV11aDivWeekendVol",
            },
        ),
        PublishedTargetCyclePayload(
            {"cycle": "captured"},
            publication=publication,
            route=route,
        ),
        sleeve="long",
    )

    safety_capture_path = source / "post-window-safety" / "capture.jsonl"
    safety_publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix="natural-safety-empty",
        exit_intents=(),
        entry_intents=(),
        created_ts_ns=NATURAL_T1_NS + 1,
    )
    JsonlTargetSchedulingCaptureTape(safety_capture_path).append_from_cycle(
        StrategyEvent(
            event_ts_ns=NATURAL_T1_NS + 1,
            ingest_ts_ns=NATURAL_T1_NS + 1,
            source="long:demo",
            source_sequence=0,
            kind="timer",
            payload={
                "execution_environment": "demo",
                "strategy_profile": POST_WINDOW_SAFETY_STRATEGY_PROFILE,
                "natural_safety_flatten": True,
                "natural_freeze_id": "fixture-natural-epoch",
                "natural_t1_ns": NATURAL_T1_NS,
                "account_id": route.account_id,
                "route_id": route.route_id,
                "journal_sequence": 0,
                "journal_state_hash": GENESIS_HASH,
                "scope": POST_WINDOW_SAFETY_SCOPE,
            },
        ),
        PublishedTargetCyclePayload(
            {"cycle": "post-window-safety-empty"},
            publication=safety_publication,
            route=route,
        ),
        sleeve="long",
    )
    safety_capture_path.chmod(0o600)
    empty_publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix="empty-cycle",
        exit_intents=(),
        entry_intents=(),
        created_ts_ns=WALL_NS + 10,
    )
    capture_tape.append_from_cycle(
        StrategyEvent(
            event_ts_ns=WALL_NS + 10,
            ingest_ts_ns=WALL_NS + 11,
            source="long:demo",
            source_sequence=2,
            kind="timer",
            payload={
                "execution_environment": "demo",
                "strategy_profile": "LongV11aDivWeekendVol",
            },
        ),
        PublishedTargetCyclePayload(
            {"cycle": "empty"},
            publication=empty_publication,
            route=route,
        ),
        sleeve="long",
    )

    market_root = source / "market-capture"
    recorder = SequenceAwareMarketRecorder(
        market_root,
        config=MarketCaptureConfig(
            depth=50,
            segment_max_bytes=1_000_000,
            fsync_every_records=1,
            min_free_disk_bytes=1,
            ring_records_per_symbol=100,
        ),
        clock=clock,
    )
    recorder.on_message(
        {
            "topic": "orderbook.50.BTCUSDT",
            "type": "snapshot",
            "ts": 9_999,
            "cts": 9_999,
            "data": {
                "s": "BTCUSDT",
                "b": [["9.9", "100"]],
                "a": [["10.1", "100"]],
                "u": 1,
                "seq": 1,
            },
        },
        local_receive_ts_ns=WALL_NS,
    )
    captured_provider = CapturedBybitMarketProvider(recorder)
    advancing_provider = _AdvancingMarketProvider(captured_provider, clock)
    kernel = AccountExecutionKernel(
        route.account_path,
        account_id=route.account_id,
        clock=clock,
    )
    demo_twin = MarketOrderExecutionTwin(
        books={},
        instrument_rules={"BTCUSDT": rule},
        config=ExecutionTwinConfig(
            fee_bps=5.5,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=1_000,
        ),
        name="fixture_demo_model",
        id_seed="fixture-demo-execution",
    )
    service = AccountExecutionService(
        route=route,
        kernel=kernel,
        market_provider=advancing_provider,
        snapshot_provider=_SnapshotProvider(clock),
        rules_provider=VerifiedBybitDemoRulesProvider({"BTCUSDT": rule}),
        risk_policy=policy,
        execution_adapter=CapturedPaperExecutionAdapter(
            market_provider=captured_provider,
            twin=demo_twin,
        ),
        clock=clock,
        max_market_age_ns=10_000_000_000 if stale_market else 5_000_000_000,
        required_rules_environment="demo",
    )
    if stale_market:
        clock.advance_ns(6_000_000_000)
    if uncaptured_pre_window is not None:
        pre_receipt = service.handle(uncaptured_pre_window)
        assert pre_receipt.accepted
    request = publication.entry_requests[0].request
    receipt = service.handle(request)
    assert receipt.accepted
    events = read_account_journal(route.account_path, verify=True)
    market_events = [
        event
        for event in events
        if event.correlation_id == request.batch_id and event.event_type == AccountEventType.MARKET_INPUT_REF.value
    ]
    assert len(market_events) == 1
    recorder.close()
    _write_calibration(
        calibration_file,
        account_root=route.account_path,
        market_root=market_root,
    )
    safety_manifest = source / "config" / "post-window-safety-manifest.json"
    _write_empty_post_window_safety_manifest(
        safety_manifest,
        capture_path=safety_capture_path,
        freeze_id="fixture-natural-epoch",
    )
    freeze_manifest = source / "config" / "natural-cutover-freeze.json"
    _write_test_freeze(
        freeze_manifest,
        account_root=route.account_path,
        market_root=market_root,
        rules_file=rules_file,
        policy_file=policy_file,
        calibration_file=calibration_file,
    )
    effective_runtime_config_bundle = source / "config" / "effective-runtime-config-bundle.json"
    _write_test_effective_bundle(
        effective_runtime_config_bundle,
        freeze_manifest=freeze_manifest,
        target_capture=capture_path,
    )
    input_manifest = source / "config" / "natural-account-replay-input.json"
    build_natural_account_replay_input_manifest(
        target_capture_path=capture_path,
        demo_account_root=route.account_path,
        market_capture_root=market_root,
        demo_rules_file=rules_file,
        risk_policy_file=policy_file,
        calibration_file=calibration_file,
        freeze_manifest_path=freeze_manifest,
        effective_runtime_config_bundle_file=effective_runtime_config_bundle,
        safety_target_capture_path=safety_capture_path,
        safety_manifest_path=safety_manifest,
        expected_account_id=ACCOUNT_ID,
        output_path=input_manifest,
        t0_ns=NATURAL_T0_NS,
        t1_ns=NATURAL_T1_NS,
        max_decision_age_ns=250_000_000,
    )
    return _Fixture(
        source_root=source,
        target_capture=capture_path,
        account_root=route.account_path,
        market_root=market_root,
        rules_file=rules_file,
        policy_file=policy_file,
        calibration_file=calibration_file,
        freeze_manifest=freeze_manifest,
        effective_runtime_config_bundle=effective_runtime_config_bundle,
        safety_target_capture=safety_capture_path,
        safety_manifest=safety_manifest,
        input_manifest=input_manifest,
        batch_id=request.batch_id,
        input_key=str(market_events[0].payload["input_key"]),
    )


def _run(fixture: _Fixture, output: Path):
    return run_captured_account_replay(
        target_capture_path=fixture.target_capture,
        demo_account_root=fixture.account_root,
        market_capture_root=fixture.market_root,
        demo_rules_file=fixture.rules_file,
        risk_policy_file=fixture.policy_file,
        calibration_file=fixture.calibration_file,
        freeze_manifest_path=fixture.freeze_manifest,
        effective_runtime_config_bundle_file=fixture.effective_runtime_config_bundle,
        safety_target_capture_path=fixture.safety_target_capture,
        safety_manifest_path=fixture.safety_manifest,
        input_manifest_path=fixture.input_manifest,
        expected_account_id=ACCOUNT_ID,
        output_root=output,
        max_decision_age_ns=250_000_000,
    )


@pytest.mark.parametrize(
    ("override", "value", "message"),
    [
        ("max_decision_age_ns", 250_000_001, "max_decision_age_ns"),
        ("max_market_age_ns", 5_000_000_001, "max_market_age_ns"),
        ("max_snapshot_age_ns", 5_000_000_001, "max_snapshot_age_ns"),
        ("latency_quantile", "p95", "latency_quantile"),
        ("slippage_quantile", "p95", "slippage_quantile"),
        ("kernel_id_seed", "alternate-kernel", "kernel_id_seed"),
        ("twin_id_seed", "alternate-twin", "twin_id_seed"),
    ],
)
def test_account_replay_rejects_unregistered_configuration_before_sources(
    tmp_path: Path,
    override: str,
    value: object,
    message: str,
) -> None:
    arguments: dict[str, Any] = {
        "target_capture_path": tmp_path / "missing-targets.jsonl",
        "demo_account_root": tmp_path / "missing-account",
        "market_capture_root": tmp_path / "missing-capture",
        "demo_rules_file": tmp_path / "missing-rules.json",
        "risk_policy_file": tmp_path / "missing-policy.json",
        "calibration_file": tmp_path / "missing-calibration.json",
        "freeze_manifest_path": tmp_path / "missing-freeze.json",
        "effective_runtime_config_bundle_file": tmp_path / "missing-effective.json",
        "safety_target_capture_path": tmp_path / "missing-safety.jsonl",
        "safety_manifest_path": tmp_path / "missing-safety.json",
        "expected_account_id": ACCOUNT_ID,
        "output_path": tmp_path / "manifest.json",
        "t0_ns": NATURAL_T0_NS,
        "t1_ns": NATURAL_T1_NS,
        "max_decision_age_ns": 250_000_000,
        "max_market_age_ns": 5_000_000_000,
        "max_snapshot_age_ns": 5_000_000_000,
        "latency_quantile": "p50",
        "slippage_quantile": "p50",
        "kernel_id_seed": "account-kernel-v1",
        "twin_id_seed": "captured-demo-account-replay-v1:execution",
    }
    arguments[override] = value
    with pytest.raises(ValueError, match=message):
        build_natural_account_replay_input_manifest(**arguments)


def _rebuild_input_manifest(fixture: _Fixture) -> None:
    fixture.input_manifest.unlink()
    _write_test_effective_bundle(
        fixture.effective_runtime_config_bundle,
        freeze_manifest=fixture.freeze_manifest,
        target_capture=fixture.target_capture,
    )
    build_natural_account_replay_input_manifest(
        target_capture_path=fixture.target_capture,
        demo_account_root=fixture.account_root,
        market_capture_root=fixture.market_root,
        demo_rules_file=fixture.rules_file,
        risk_policy_file=fixture.policy_file,
        calibration_file=fixture.calibration_file,
        freeze_manifest_path=fixture.freeze_manifest,
        effective_runtime_config_bundle_file=fixture.effective_runtime_config_bundle,
        safety_target_capture_path=fixture.safety_target_capture,
        safety_manifest_path=fixture.safety_manifest,
        expected_account_id=ACCOUNT_ID,
        output_path=fixture.input_manifest,
        t0_ns=NATURAL_T0_NS,
        t1_ns=NATURAL_T1_NS,
        max_decision_age_ns=250_000_000,
    )


def test_captured_account_replay_preserves_exact_demo_inputs_and_separates_claims(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    output = tmp_path / "account-replay"
    receipt = _run(fixture, output)
    payload = receipt.payload

    assert receipt.path == output / ACCOUNT_REPLAY_RECEIPT_FILENAME
    assert payload["schema_version"] == replay_module.ACCOUNT_REPLAY_SCHEMA_VERSION
    assert type(payload["created_ts_ns"]) is int and payload["created_ts_ns"] > 0
    assert payload["target_capture"] == {
        "capture_tape_hash": payload["target_capture"]["capture_tape_hash"],
        "event_count": 2,
        "successful_empty_event_count": 1,
        "durable_request_count": 1,
    }
    assert payload["ordered_batch_ids"] == [fixture.batch_id]
    assert payload["request_batch_mappings"][0]["market_inputs"][0]["input_key"] == (fixture.input_key)
    assert payload["historical_paper_exact_outcome_passed"] is True
    assert payload["demo_plan_parity_passed"] is True
    assert payload["kernel_parity_report"]["passed"] is True
    assert payload["kernel_parity_report"]["scoped_batch_ids"] == [fixture.batch_id]
    assert payload["exact_preexecution_plan_match"] is True
    assert payload["input_manifest"]["natural_window"] == {
        "t0_ns": NATURAL_T0_NS,
        "t1_ns": NATURAL_T1_NS,
    }
    assert payload["input_manifest"]["natural_epoch_identity"]["epoch_separation_passed"] is True
    assert payload["post_window_safety"]["capture_event_count"] == 1
    assert payload["post_window_safety"]["durable_request_count"] == 0
    assert payload["post_window_safety"]["journal_classification"]["batch_set_exact"] is True
    assert payload["post_window_safety"]["excluded_from_natural_replay_parity_and_lifecycle_floors"] is True
    assert payload["natural_tape_sufficiency"]["registered_floor_checks"]["fixed_half_open_120h_window"] is True
    assert payload["natural_tape_sufficiency"]["sufficiency_gate_passed"] is False
    assert payload["natural_tape_sufficiency"]["status"] == ("separate_natural_tape_sufficiency_verifier_required")
    assert payload["scheduling_parity_claim"] == "separate_artifact_required"
    assert payload["actual_demo_execution_evidence_claim"] == ("separate_actual_venue_gate_required")
    assert payload["execution_authorization"] == "not_granted"
    assert not (output / "historical" / "account_route.json").exists()
    assert not (output / "paper" / "account_route.json").exists()
    assert not (output / "historical" / "pending").exists()
    assert load_captured_account_replay_receipt(receipt.path) == payload

    demo_events = read_account_journal(fixture.account_root, verify=True)
    historical_events = read_account_journal(output / "historical", verify=True)
    demo_market = next(
        event
        for event in demo_events
        if event.correlation_id == fixture.batch_id and event.event_type == AccountEventType.MARKET_INPUT_REF.value
    )
    historical_market = next(
        event
        for event in historical_events
        if event.correlation_id == fixture.batch_id and event.event_type == AccountEventType.MARKET_INPUT_REF.value
    )
    demo_risk = next(
        event
        for event in demo_events
        if event.correlation_id == fixture.batch_id and event.event_type == AccountEventType.RISK_DECISION.value
    )
    historical_risk = next(
        event
        for event in historical_events
        if event.correlation_id == fixture.batch_id and event.event_type == AccountEventType.RISK_DECISION.value
    )
    assert historical_market.payload == demo_market.payload
    assert (historical_risk.wall_ts_ns, historical_risk.monotonic_ns) == (
        demo_risk.wall_ts_ns,
        demo_risk.monotonic_ns,
    )
    assert historical_risk.payload == demo_risk.payload


def _rewrite_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    updated = dict(payload)
    updated["artifact_sha256"] = hashlib.sha256(
        canonical_json({**updated, "artifact_sha256": ""})
    ).hexdigest()
    path.chmod(0o600)
    path.write_bytes(canonical_json(updated) + b"\n")
    path.chmod(0o400)
    created_ts_ns = int(updated["created_ts_ns"])
    os.utime(path, ns=(created_ts_ns, created_ts_ns))


@pytest.mark.parametrize(
    "source_name",
    (
        "target_capture",
        "demo_transaction",
        "market_capture",
        "demo_rules",
        "risk_policy",
        "calibration",
        "freeze_manifest",
        "effective_runtime_config",
        "safety_target_capture",
        "safety_manifest",
        "input_manifest",
    ),
)
def test_loader_reopens_every_bound_source_file(
    tmp_path: Path,
    source_name: str,
) -> None:
    fixture = _build_fixture(tmp_path)
    receipt = _run(fixture, tmp_path / "account-replay")
    bound_sources = receipt.payload["source_files"]
    sources = {
        "target_capture": fixture.target_capture,
        "demo_transaction": Path(
            next(
                value["path"]
                for label, value in bound_sources.items()
                if label.startswith("demo_journal_transaction/")
            )
        ),
        "market_capture": Path(
            next(
                value["path"]
                for label, value in bound_sources.items()
                if label.startswith("market_capture/")
            )
        ),
        "demo_rules": fixture.rules_file,
        "risk_policy": fixture.policy_file,
        "calibration": fixture.calibration_file,
        "freeze_manifest": fixture.freeze_manifest,
        "effective_runtime_config": fixture.effective_runtime_config_bundle,
        "safety_target_capture": fixture.safety_target_capture,
        "safety_manifest": fixture.safety_manifest,
        "input_manifest": fixture.input_manifest,
    }
    selected = sources[source_name]
    selected.write_bytes(selected.read_bytes() + b" ")
    with pytest.raises(ValueError, match="bound source files changed"):
        load_captured_account_replay_receipt(receipt.path)


def test_loader_recomputes_claimed_gates_instead_of_trusting_rehashed_receipt(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    receipt = _run(fixture, tmp_path / "account-replay")
    forged = dict(receipt.payload)
    forged["historical_paper_exact_outcome_passed"] = False
    forged["demo_plan_parity_passed"] = False
    forged["exact_preexecution_plan_match"] = False
    forged["has_durable_request_batches"] = False
    forged["kernel_parity_report"] = {
        **dict(forged["kernel_parity_report"]),
        "passed": False,
    }
    forged["natural_tape_sufficiency"] = {
        **dict(forged["natural_tape_sufficiency"]),
        "sufficiency_gate_passed": True,
    }
    _rewrite_receipt(receipt.path, forged)
    with pytest.raises(ValueError, match="claims do not reproduce"):
        load_captured_account_replay_receipt(receipt.path)


def test_loader_recomputes_source_semantics_after_forged_identity_update(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    receipt = _run(fixture, tmp_path / "account-replay")
    capture_bytes = fixture.target_capture.read_bytes()
    assert capture_bytes.endswith(b"\n")
    fixture.target_capture.write_bytes(capture_bytes[:-1] + b" \n")
    changed_identity, _data = replay_module._read_frozen_file(
        fixture.target_capture,
        label="target_scheduling_capture",
    )
    forged = json.loads(json.dumps(receipt.payload))
    forged["source_files"]["target_scheduling_capture"] = changed_identity.to_dict()
    _rewrite_receipt(receipt.path, forged)
    with pytest.raises(
        ValueError,
        match="input manifest does not match the exact frozen inputs",
    ):
        load_captured_account_replay_receipt(receipt.path)


@pytest.mark.parametrize("profile", ("historical", "paper"))
def test_loader_rejects_mutated_modeled_output_tree(
    tmp_path: Path,
    profile: str,
) -> None:
    fixture = _build_fixture(tmp_path)
    receipt = _run(fixture, tmp_path / "account-replay")
    tape = receipt.path.parent / profile / "strategy_event_tape.jsonl"
    original_stat = tape.stat()
    tape.write_bytes(tape.read_bytes() + b" ")
    os.utime(tape, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    with pytest.raises(ValueError, match="modeled outputs changed"):
        load_captured_account_replay_receipt(receipt.path)


def test_loader_rejects_rehashed_output_moved_inside_a_source_root(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    receipt = _run(fixture, tmp_path / "account-replay")
    moved_root = fixture.account_root / "forged-replay-output"
    shutil.move(str(receipt.path.parent), moved_root)
    moved_receipt = moved_root / ACCOUNT_REPLAY_RECEIPT_FILENAME
    forged = json.loads(moved_receipt.read_bytes())
    forged["outputs"] = {
        **dict(forged["outputs"]),
        "historical_root": str(moved_root / "historical"),
        "paper_root": str(moved_root / "paper"),
    }
    _rewrite_receipt(moved_receipt, forged)
    with pytest.raises(ValueError, match="output overlaps source root"):
        load_captured_account_replay_receipt(moved_receipt)


def test_loader_requires_creation_timestamp_to_match_receipt_metadata(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    receipt = _run(fixture, tmp_path / "account-replay")
    os.utime(
        receipt.path,
        ns=(
            int(receipt.payload["created_ts_ns"]) + 1,
            int(receipt.payload["created_ts_ns"]) + 1,
        ),
    )
    with pytest.raises(ValueError, match="not bound to receipt metadata"):
        load_captured_account_replay_receipt(receipt.path)


def test_output_must_be_fresh_and_disjoint_from_every_source_root(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    with pytest.raises(ValueError, match="overlaps source root"):
        _run(fixture, fixture.account_root / "nested-output")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        _run(fixture, existing)


def test_missing_selected_book_context_is_rejected_even_with_rebound_calibration(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    for segment in fixture.market_root.rglob("segment-*.jsonl"):
        rows = [json.loads(line) for line in segment.read_bytes().splitlines()]
        kept = [row for row in rows if row.get("record_id") != fixture.input_key]
        segment.write_bytes(b"".join(canonical_json(row) + b"\n" for row in kept))
    _rebuild_input_manifest(fixture)
    with pytest.raises(ValueError, match="missing from raw capture"):
        _run(fixture, tmp_path / "missing-context-output")


def test_duplicate_raw_context_identity_is_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    segment = next(fixture.market_root.rglob("segment-*.jsonl"))
    rows = segment.read_bytes().splitlines()
    selected = next(line for line in rows if json.loads(line).get("record_id") == fixture.input_key)
    segment.write_bytes(segment.read_bytes() + selected + b"\n")
    _rebuild_input_manifest(fixture)
    with pytest.raises(ValueError, match="duplicate market capture record id"):
        _run(fixture, tmp_path / "duplicate-context-output")


def test_risk_policy_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    wrong_policy = AccountRiskPolicy(999.0, 2_000.0, 1_000.0, 1_000.0, 10.0)
    fixture.policy_file.write_bytes(canonical_json(asdict(wrong_policy)) + b"\n")
    _write_test_freeze(
        fixture.freeze_manifest,
        account_root=fixture.account_root,
        market_root=fixture.market_root,
        rules_file=fixture.rules_file,
        policy_file=fixture.policy_file,
        calibration_file=fixture.calibration_file,
    )
    _rebuild_input_manifest(fixture)
    with pytest.raises(ValueError, match="does not use the supplied risk policy"):
        _run(fixture, tmp_path / "wrong-policy-output")


def test_source_mutation_aborts_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_fixture(tmp_path)
    output = tmp_path / "mutated-output"
    original = HistoricalAccountReplay.run
    calls = 0

    def mutating_run(self: HistoricalAccountReplay, cycles: Sequence[Any]):
        nonlocal calls
        result = original(self, cycles)
        calls += 1
        if calls == 1:
            fixture.target_capture.write_bytes(fixture.target_capture.read_bytes() + b" ")
        return result

    monkeypatch.setattr(HistoricalAccountReplay, "run", mutating_run)
    with pytest.raises(RuntimeError, match="source files changed"):
        _run(fixture, output)
    assert not output.exists()


def test_v7_training_epoch_must_be_disjoint_from_the_natural_holdout(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    value = json.loads(fixture.calibration_file.read_bytes())
    value["inputs"]["account_journal_sha256"] = _journal_sha(read_account_journal(fixture.account_root, verify=True))
    value["artifact_sha256"] = hashlib.sha256(canonical_json({**value, "artifact_sha256": ""})).hexdigest()
    fixture.calibration_file.write_bytes(canonical_json(value) + b"\n")
    _write_test_freeze(
        fixture.freeze_manifest,
        account_root=fixture.account_root,
        market_root=fixture.market_root,
        rules_file=fixture.rules_file,
        policy_file=fixture.policy_file,
        calibration_file=fixture.calibration_file,
    )
    with pytest.raises(ValueError, match="reuse the same journal bytes"):
        _rebuild_input_manifest(fixture)


def test_top_level_freeze_window_must_match_replay_manifest(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    freeze = json.loads(fixture.freeze_manifest.read_text(encoding="utf-8"))
    freeze["window"]["t1_ns"] += 1
    fixture.freeze_manifest.write_bytes(canonical_json(freeze) + b"\n")
    fixture.freeze_manifest.chmod(0o600)
    with pytest.raises(ValueError, match="T0/T1 differ"):
        _rebuild_input_manifest(fixture)


def test_freeze_source_mutation_aborts_atomic_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_fixture(tmp_path)
    output = tmp_path / "freeze-mutated-output"
    original = HistoricalAccountReplay.run
    calls = 0

    def mutating_run(self: HistoricalAccountReplay, cycles: Sequence[Any]):
        nonlocal calls
        result = original(self, cycles)
        calls += 1
        if calls == 1:
            fixture.freeze_manifest.write_bytes(fixture.freeze_manifest.read_bytes() + b" ")
        return result

    monkeypatch.setattr(HistoricalAccountReplay, "run", mutating_run)
    with pytest.raises(RuntimeError, match="source files changed"):
        _run(fixture, output)
    assert not output.exists()


def test_post_window_safety_manifest_must_match_its_exact_capture(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    value = json.loads(fixture.safety_manifest.read_bytes())
    value["capture_event_count"] = int(value["capture_event_count"]) + 1
    value["artifact_sha256"] = hashlib.sha256(canonical_json({**value, "artifact_sha256": ""})).hexdigest()
    fixture.safety_manifest.write_bytes(canonical_json(value) + b"\n")
    fixture.safety_manifest.chmod(0o600)
    with pytest.raises(ValueError, match="does not match its exact target capture"):
        _rebuild_input_manifest(fixture)


def test_request_preparation_is_shared_with_the_production_service(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    _run(fixture, tmp_path / "shared-transform-output")
    demo_target = next(
        event
        for event in read_account_journal(fixture.account_root, verify=True)
        if event.correlation_id == fixture.batch_id and event.event_type == AccountEventType.TARGET.value
    )
    replay_target = next(
        event
        for event in read_account_journal(tmp_path / "shared-transform-output" / "historical", verify=True)
        if event.correlation_id == fixture.batch_id and event.event_type == AccountEventType.TARGET.value
    )
    assert (
        demo_target.payload["metadata"]["account_request_id"]
        == (replay_target.payload["metadata"]["account_request_id"])
    )
    assert (
        demo_target.payload["metadata"]["account_request_created_ts_ns"]
        == (replay_target.payload["metadata"]["account_request_created_ts_ns"])
    )


def test_pre_window_request_batch_rejects_a_nonfresh_demo_epoch(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, pre_window_request=True)
    with pytest.raises(ValueError, match="pre-window strategy request batches"):
        _run(fixture, tmp_path / "pre-window-output")


def test_exact_demo_clocks_reject_stale_input_instead_of_retiming_it(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, stale_market=True)
    with pytest.raises(ValueError, match="stale/future market input"):
        _run(fixture, tmp_path / "stale-output")
