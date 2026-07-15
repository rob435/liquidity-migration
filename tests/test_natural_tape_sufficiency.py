from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.captured_account_replay as replay_module
import liquidity_migration.natural_tape_sufficiency as sufficiency_module
from liquidity_migration.account_intent_client import (
    AccountTargetPublisher,
    component_target_key,
    publish_exit_first_target_requests,
    requested_target,
)
from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
    MarketInputRef,
    read_account_journal,
)
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.account_service import (
    SleeveAdapterKind,
    prepare_account_request_intents,
)
from liquidity_migration.account_venue_accounting import (
    VenueAccountingRequirements,
    build_venue_accounting_receipt,
    write_venue_accounting_receipt,
)
from liquidity_migration.captured_account_replay import (
    POST_WINDOW_SAFETY_SCOPE,
    POST_WINDOW_SAFETY_STRATEGY_PROFILE,
    build_post_window_safety_manifest,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.natural_tape_sufficiency import (
    HOUR_NS,
    build_natural_tape_sufficiency_receipt,
    load_natural_tape_sufficiency_receipt,
    write_natural_tape_sufficiency_receipt,
)
from liquidity_migration.strategy_event_clock import JsonlStrategyEventTape, StrategyEvent
from liquidity_migration.strategy_event_outcome import JsonlStrategyEventDecisionTape
from liquidity_migration.strategy_runtime import AccountKernelRuntime, AdaptedIntent
from liquidity_migration.strategy_target_replay import (
    JsonlTargetSchedulingCaptureTape,
    PublishedTargetCyclePayload,
    load_target_scheduling_capture,
)


ACCOUNT_ID = "natural-sufficiency-demo"
T0_NS = HOUR_NS
T1_NS = T0_NS + 120 * HOUR_NS


@dataclass(frozen=True)
class _Fixture:
    long_event_tape: Path
    long_outcome_tape: Path
    continuous_event_tape: Path
    continuous_outcome_tape: Path
    target_capture: Path
    account_root: Path
    safety_capture: Path
    safety_manifest: Path
    freeze_manifest: Path
    effective_runtime_config_bundle: Path
    replay_receipt: Path
    venue_receipt: Path
    batch_ids: tuple[str, ...]


@pytest.fixture(autouse=True)
def _load_test_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sufficiency_module,
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
        sufficiency_module,
        "load_effective_runtime_config_bundle_binding",
        load_effective_bundle,
    )
    monkeypatch.setattr(
        replay_module,
        "load_effective_runtime_config_bundle_binding",
        load_effective_bundle,
    )

    def load_replay_receipt(path: str | Path) -> dict[str, Any]:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return replay_module.verify_captured_account_replay_receipt(raw)

    # Captured replay has its own exhaustive source/output rerun tests. These
    # sufficiency tests isolate the downstream joins while retaining the exact
    # current replay schema and self-hash validation.
    monkeypatch.setattr(
        sufficiency_module,
        "load_captured_account_replay_receipt",
        load_replay_receipt,
    )


def _write_test_freeze(
    path: Path,
    *,
    account_root: Path,
    capture_root: Path,
) -> None:
    candidate_file = path.parent / "candidate-universe.json"
    candidate_file.parent.mkdir(parents=True, exist_ok=True)
    if not candidate_file.exists():
        candidate_file.write_bytes(b'{"symbols":["BTCUSDT"]}\n')
        candidate_file.chmod(0o600)
    payload = {
        "artifact_sha256": hashlib.sha256(b"natural-test-freeze").hexdigest(),
        "freeze_id": "natural-fixture-freeze",
        "repository": {
            "candidate_commit": "a" * 40,
            "origin_main_commit": "b" * 40,
        },
        "window": {"t0_ns": T0_NS, "t1_ns": T1_NS},
        "runtime": {
            "account_ids": {"demo": ACCOUNT_ID, "paper": "fixture-paper"},
            "roots": {
                "demo": {
                    "account": str(account_root.resolve()),
                    "capture": str(capture_root.resolve()),
                }
            },
        },
        "clock": {
            "receipt": {
                "artifact_sha256": hashlib.sha256(b"clock-artifact").hexdigest(),
                "file_sha256": hashlib.sha256(b"clock-file").hexdigest(),
            }
        },
        "population": {
            "candidate_universe": {
                "path": str(candidate_file.resolve()),
                "file_sha256": hashlib.sha256(candidate_file.read_bytes()).hexdigest(),
                "artifact_sha256": hashlib.sha256(b"candidate-artifact").hexdigest(),
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
    long_event_tape: Path,
    long_outcome_tape: Path,
    continuous_event_tape: Path,
    continuous_outcome_tape: Path,
) -> None:
    freeze = json.loads(freeze_manifest.read_text(encoding="utf-8"))
    run_config = path.parent / "natural-run-config.json"
    run_config.write_bytes(b'{"fixture":"natural-run-config"}\n')
    run_config.chmod(0o600)
    binding = {
        "artifact_sha256": hashlib.sha256(b"effective-config-artifact").hexdigest(),
        "validator": "natural_effective_runtime_config_bundle_v2",
        "created_ts_ns": T0_NS - 1,
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
        "candidate_universe": dict(freeze["population"]["candidate_universe"]),
        "window": {
            "t0_ns": freeze["window"]["t0_ns"],
            "t1_ns": freeze["window"]["t1_ns"],
            "interval": "half_open_[t0,t1)",
        },
        "runtime_paths": {
            "target_capture_path": str(target_capture.resolve()),
            "sleeves": {
                "long": {
                    "event_tape_path": str(long_event_tape.resolve()),
                    "outcome_tape_path": str(long_outcome_tape.resolve()),
                },
                "continuous": {
                    "event_tape_path": str(continuous_event_tape.resolve()),
                    "outcome_tape_path": str(continuous_outcome_tape.resolve()),
                },
            },
        },
        "receipts": {},
        "execution_authorization": "not_granted",
    }
    path.write_bytes(canonical_json(binding) + b"\n")
    path.chmod(0o600)


def _profile(sleeve: str) -> str:
    return "LongV11aDivWeekendVol" if sleeve == "long" else "continuous_ensemble_v2"


def _event(*, sleeve: str, sequence: int, event_ts_ns: int) -> StrategyEvent:
    return StrategyEvent(
        event_ts_ns=event_ts_ns,
        ingest_ts_ns=event_ts_ns + 1,
        source=f"{sleeve}:demo",
        source_sequence=sequence,
        kind="startup" if sequence == 1 else "timer",
        payload={
            "execution_environment": "demo",
            "strategy_profile": _profile(sleeve),
            "natural_evidence_required": True,
        },
    )


def _journal_sha(events: list[Any]) -> str:
    return hashlib.sha256(canonical_json({"events": [event.to_dict() for event in events]})).hexdigest()


def _source_identity(label: str, path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    observed = resolved.stat()
    data = resolved.read_bytes()
    return {
        "label": label,
        "path": str(resolved),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "device": observed.st_dev,
        "inode": observed.st_ino,
        "mtime_ns": observed.st_mtime_ns,
        "mode": observed.st_mode & 0o777,
    }


def _write_replay_receipt(
    path: Path,
    *,
    target_capture: Path,
    account_root: Path,
    safety_capture: Path,
    safety_manifest: Path,
    freeze_manifest: Path,
    effective_runtime_config_bundle: Path,
    market_capture_root: Path,
    historical_root: Path,
    paper_root: Path,
    requests: tuple[Any, ...],
    event_count: int,
) -> None:
    source_files: dict[str, dict[str, Any]] = {
        "target_scheduling_capture": _source_identity("target_scheduling_capture", target_capture),
        "post_window_safety_target_capture": _source_identity("post_window_safety_target_capture", safety_capture),
        "post_window_safety_manifest": _source_identity("post_window_safety_manifest", safety_manifest),
        "natural_cutover_freeze_manifest": _source_identity("natural_cutover_freeze_manifest", freeze_manifest),
        "effective_runtime_config_bundle": _source_identity(
            "effective_runtime_config_bundle",
            effective_runtime_config_bundle,
        ),
    }
    transaction_root = account_root / "account_journal" / "transactions"
    for transaction in sorted(transaction_root.glob("*.json")):
        label = f"demo_journal_transaction/{transaction.name}"
        source_files[label] = _source_identity(label, transaction)
    projection = account_root / "account_journal" / "events.jsonl"
    source_files["demo_journal_projection/events.jsonl"] = _source_identity(
        "demo_journal_projection/events.jsonl", projection
    )
    historical_events = read_account_journal(historical_root, verify=True)
    paper_events = read_account_journal(paper_root, verify=True)
    payload: dict[str, Any] = {
        "schema_version": replay_module.ACCOUNT_REPLAY_SCHEMA_VERSION,
        "kind": replay_module.ACCOUNT_REPLAY_KIND,
        "created_ts_ns": T1_NS + 1,
        "source_roots": {
            "target_capture_path": str(target_capture.resolve()),
            "demo_account_root": str(account_root.resolve()),
            "market_capture_root": str(market_capture_root.resolve()),
            "natural_cutover_freeze_manifest_path": str(freeze_manifest.resolve()),
            "effective_runtime_config_bundle_file": str(
                effective_runtime_config_bundle.resolve()
            ),
            "post_window_safety_target_capture_path": str(safety_capture.resolve()),
            "post_window_safety_manifest_path": str(safety_manifest.resolve()),
        },
        "source_files": source_files,
        "natural_cutover_freeze": {
            "path": str(freeze_manifest.resolve()),
            "freeze_id": "natural-fixture-freeze",
            "artifact_sha256": hashlib.sha256(b"natural-test-freeze").hexdigest(),
        },
        "effective_runtime_config": replay_module.load_effective_runtime_config_bundle_binding(
            effective_runtime_config_bundle
        )[1],
        "input_manifest": {"natural_window": {"t0_ns": T0_NS, "t1_ns": T1_NS}},
        "ordered_batch_ids": [request.batch_id for request in requests],
        "target_capture": {
            "event_count": event_count,
            "durable_request_count": len(requests),
        },
        "post_window_safety": {
            "journal_classification": {
                "registered_batch_ids": [],
                "journal_batch_ids": [],
                "batch_set_exact": True,
                "batches": [],
            }
        },
        "request_batch_mappings": [
            {
                "batch_id": request.batch_id,
                "request_id": request.request_id,
                "request_hash": request.content_hash(),
            }
            for request in requests
        ],
        "outputs": {
            "historical_root": str(historical_root.resolve()),
            "paper_root": str(paper_root.resolve()),
            "historical_account_journal_sha256": _journal_sha(historical_events),
            "paper_account_journal_sha256": _journal_sha(paper_events),
        },
        "historical_paper_exact_outcome_passed": True,
        "demo_plan_parity_passed": True,
        "exact_preexecution_plan_match": True,
        "has_durable_request_batches": True,
        "execution_authorization": "not_granted",
        "limitations": list(replay_module._LIMITATIONS),
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = replay_module._self_hash(payload)
    replay_module.verify_captured_account_replay_receipt(payload)
    path.write_bytes(canonical_json(payload) + b"\n")
    path.chmod(0o600)


def _fixture(tmp_path: Path) -> _Fixture:
    route = ensure_account_route(
        account_id=ACCOUNT_ID,
        environment="demo",
        account_root=tmp_path / "demo-account",
        inbox_root=tmp_path / "demo-inbox",
    )
    publisher = AccountTargetPublisher(
        route,
        clock=VirtualClock(current_wall_ns=T0_NS + 1),
    )
    target = requested_target(
        adapter_kind=SleeveAdapterKind.LONG,
        decision_key="natural/long/entry",
        target_key=component_target_key(
            sleeve=SleeveAdapterKind.LONG,
            strategy_id="natural-fixture",
            component_id="main",
            symbol="BTCUSDT",
        ),
        strategy_id="natural-fixture",
        component_id="main",
        symbol="BTCUSDT",
        signed_notional_usdt=100.0,
        leverage=2.0,
        reason="natural fixture entry",
    )
    entry_publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix="natural-fixture-entry",
        exit_intents=(),
        entry_intents=(target,),
        created_ts_ns=T0_NS + 1,
    )
    assert len(entry_publication.entry_requests) == 1
    entry_request = entry_publication.entry_requests[0].request
    exit_target = requested_target(
        adapter_kind=SleeveAdapterKind.LONG,
        decision_key="natural/long/exit",
        target_key=target.intent.target_key,
        strategy_id="natural-fixture",
        component_id="main",
        symbol="BTCUSDT",
        signed_notional_usdt=0.0,
        leverage=2.0,
        reason="natural fixture exit",
    )
    exit_event_ts_ns = T0_NS + HOUR_NS + 1
    exit_publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix="natural-fixture-exit",
        exit_intents=(exit_target,),
        entry_intents=(),
        created_ts_ns=exit_event_ts_ns,
    )
    assert len(exit_publication.exit_requests) == 1
    exit_request = exit_publication.exit_requests[0].request

    target_capture = tmp_path / "natural" / "target-capture.jsonl"
    capture_tape = JsonlTargetSchedulingCaptureTape(target_capture)
    event_paths = {sleeve: tmp_path / "natural" / sleeve / "strategy-events.jsonl" for sleeve in ("long", "continuous")}
    outcome_paths = {
        sleeve: tmp_path / "natural" / sleeve / "strategy-outcomes.jsonl" for sleeve in ("long", "continuous")
    }
    for hour in range(120):
        for sleeve in ("long", "continuous"):
            event = _event(
                sleeve=sleeve,
                sequence=hour + 1,
                event_ts_ns=T0_NS + hour * HOUR_NS + 1,
            )
            JsonlStrategyEventTape(event_paths[sleeve]).append(event)
            cycle_publication = (
                entry_publication
                if sleeve == "long" and hour == 0
                else exit_publication
                if sleeve == "long" and hour == 1
                else publish_exit_first_target_requests(
                    publisher,
                    batch_prefix=f"empty/{sleeve}/{hour}",
                    exit_intents=(),
                    entry_intents=(),
                    created_ts_ns=event.event_ts_ns,
                )
            )
            captured = capture_tape.append_from_cycle(
                event,
                PublishedTargetCyclePayload(
                    {"fixture": True},
                    publication=cycle_publication,
                    route=route,
                ),
                sleeve=sleeve,
            )
            JsonlStrategyEventDecisionTape(outcome_paths[sleeve]).append(
                event.event_id,
                captured.decision_keys,
            )

    market = MarketInputRef(
        input_key="natural-fixture-market",
        symbol="BTCUSDT",
        exchange_ts_ns=T0_NS,
        local_receive_ts_ns=T0_NS,
        reference_price=10.0,
        bid_price=9.9,
        ask_price=10.1,
        book_sequence=1,
        source="fixture",
    )
    rule = InstrumentRules(
        symbol="BTCUSDT",
        qty_step=0.1,
        min_qty=0.1,
        min_notional=1.0,
        tick_size=0.1,
        max_order_qty=1_000.0,
        max_leverage=10.0,
        source="fixture",
        environment="demo",
        observed_ts_ns=T0_NS,
    )
    clock = VirtualClock(current_wall_ns=T0_NS + 2, current_monotonic_ns=1)
    kernel = AccountExecutionKernel(route.account_path, account_id=ACCOUNT_ID, clock=clock)
    runtime = AccountKernelRuntime(kernel)
    prepared = prepare_account_request_intents(entry_request)
    entry_result = runtime.process_cycle(
        batch_id=entry_request.batch_id,
        intents=[AdaptedIntent(item.adapter(), intent) for item, intent in prepared],
        market_inputs={"BTCUSDT": market},
        risk_snapshot=AccountRiskSnapshot(
            equity_usdt=1_000.0,
            available_margin_usdt=1_000.0,
            snapshot_key="natural-fixture-snapshot",
            snapshot_ts_ns=T0_NS + 2,
        ),
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={"BTCUSDT": rule},
        execution_adapter=None,
        command_symbols={"BTCUSDT"},
        request_content_hash=entry_request.content_hash(),
    )
    entry_command = entry_result.target_result.commands[0]
    kernel.record_ack(
        command_id=entry_command.command_id,
        accepted=True,
        venue_order_id="venue-natural-entry",
        exchange_ts_ns=T0_NS + 3,
        local_ack_ts_ns=T0_NS + 4,
        metadata={"local_socket_send_ts_ns": T0_NS + 2},
    )
    kernel.record_fill(
        command_id=entry_command.command_id,
        execution_id="execution-natural-entry",
        signed_qty=10.0,
        price=10.0,
        fee_usdt=0.05,
        exchange_ts_ns=T0_NS + 5,
        local_receive_ts_ns=T0_NS + 6,
        metadata={
            "fee_observed": True,
            "fee_status": "observed_execution_fee",
            "fee_source": "bybit_execution_stream",
        },
    )

    clock.advance_to_wall_ns(exit_event_ts_ns + 1)
    exit_market = MarketInputRef(
        input_key="natural-fixture-exit-market",
        symbol="BTCUSDT",
        exchange_ts_ns=exit_event_ts_ns,
        local_receive_ts_ns=exit_event_ts_ns,
        reference_price=11.0,
        bid_price=10.9,
        ask_price=11.1,
        book_sequence=2,
        source="fixture",
    )
    prepared_exit = prepare_account_request_intents(exit_request)
    exit_result = runtime.process_cycle(
        batch_id=exit_request.batch_id,
        intents=[AdaptedIntent(item.adapter(), intent) for item, intent in prepared_exit],
        market_inputs={"BTCUSDT": exit_market},
        risk_snapshot=AccountRiskSnapshot(
            equity_usdt=1_000.0,
            available_margin_usdt=1_000.0,
            snapshot_key="natural-fixture-exit-snapshot",
            snapshot_ts_ns=exit_event_ts_ns + 1,
        ),
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={"BTCUSDT": rule},
        execution_adapter=None,
        command_symbols={"BTCUSDT"},
        request_content_hash=exit_request.content_hash(),
    )
    exit_command = exit_result.target_result.commands[0]
    kernel.record_ack(
        command_id=exit_command.command_id,
        accepted=True,
        venue_order_id="venue-natural-exit",
        exchange_ts_ns=exit_event_ts_ns + 2,
        local_ack_ts_ns=exit_event_ts_ns + 3,
        metadata={"local_socket_send_ts_ns": exit_event_ts_ns + 1},
    )
    kernel.record_fill(
        command_id=exit_command.command_id,
        execution_id="execution-natural-exit",
        signed_qty=-10.0,
        price=11.0,
        fee_usdt=0.055,
        exchange_ts_ns=exit_event_ts_ns + 4,
        local_receive_ts_ns=exit_event_ts_ns + 5,
        metadata={
            "fee_observed": True,
            "fee_status": "observed_execution_fee",
            "fee_source": "bybit_execution_stream",
        },
    )
    kernel.finalize_flat_position(
        symbol="BTCUSDT",
        command_id=exit_command.command_id,
        exchange_ts_ns=exit_event_ts_ns + 4,
        local_receive_ts_ns=exit_event_ts_ns + 5,
        metadata={"venue_position_confirmed_flat": True},
    )
    funding_ts_ns = (exit_event_ts_ns // 1_000_000 + 1) * 1_000_000
    kernel.record_pnl(
        pnl_key="natural-fixture-funding",
        close_key="",
        symbol="BTCUSDT",
        gross_pnl_usdt=0.0,
        fee_usdt=0.0,
        funding_usdt=0.02,
        net_pnl_usdt=0.02,
        exchange_ts_ns=funding_ts_ns,
        local_receive_ts_ns=funding_ts_ns + 1,
        source="venue_funding_settlement",
        metadata={"venue_transaction_id": "natural-fixture-settlement"},
    )

    safety_capture = tmp_path / "natural" / "safety-capture.jsonl"
    safety_event = StrategyEvent(
        event_ts_ns=T1_NS + 1,
        ingest_ts_ns=T1_NS + 1,
        source="long:demo",
        source_sequence=0,
        kind="timer",
        payload={
            "execution_environment": "demo",
            "strategy_profile": POST_WINDOW_SAFETY_STRATEGY_PROFILE,
            "natural_safety_flatten": True,
            "natural_freeze_id": "natural-fixture-freeze",
            "natural_t1_ns": T1_NS,
            "account_id": ACCOUNT_ID,
            "route_id": route.route_id,
            "journal_sequence": read_account_journal(route.account_path)[-1].sequence,
            "journal_state_hash": read_account_journal(route.account_path)[-1].state_hash,
            "scope": POST_WINDOW_SAFETY_SCOPE,
        },
    )
    empty_safety = publish_exit_first_target_requests(
        publisher,
        batch_prefix="natural-safety-empty",
        exit_intents=(),
        entry_intents=(),
        created_ts_ns=T1_NS + 1,
    )
    JsonlTargetSchedulingCaptureTape(safety_capture).append_from_cycle(
        safety_event,
        PublishedTargetCyclePayload(
            {"fixture": True},
            publication=empty_safety,
            route=route,
        ),
        sleeve="long",
    )
    safety_manifest = tmp_path / "natural" / "safety-manifest.json"
    build_post_window_safety_manifest(
        target_capture_path=safety_capture,
        expected_account_id=ACCOUNT_ID,
        freeze_id="natural-fixture-freeze",
        t1_ns=T1_NS,
        output_path=safety_manifest,
    )
    market_capture_root = tmp_path / "natural" / "market-capture"
    market_capture_root.mkdir(mode=0o700)
    freeze_manifest = tmp_path / "natural" / "cutover-freeze.json"
    _write_test_freeze(
        freeze_manifest,
        account_root=route.account_path,
        capture_root=market_capture_root,
    )
    effective_runtime_config_bundle = (
        tmp_path / "natural" / "effective-runtime-config-bundle.json"
    )
    _write_test_effective_bundle(
        effective_runtime_config_bundle,
        freeze_manifest=freeze_manifest,
        target_capture=target_capture,
        long_event_tape=event_paths["long"],
        long_outcome_tape=outcome_paths["long"],
        continuous_event_tape=event_paths["continuous"],
        continuous_outcome_tape=outcome_paths["continuous"],
    )

    historical_root = tmp_path / "replay" / "historical"
    paper_root = tmp_path / "replay" / "paper"
    shutil.copytree(route.account_path, historical_root)
    shutil.copytree(route.account_path, paper_root)
    replay_receipt = tmp_path / "replay" / "captured-account-replay.json"
    captures, _capture_hash = load_target_scheduling_capture(target_capture)
    _write_replay_receipt(
        replay_receipt,
        target_capture=target_capture,
        account_root=route.account_path,
        safety_capture=safety_capture,
        safety_manifest=safety_manifest,
        freeze_manifest=freeze_manifest,
        effective_runtime_config_bundle=effective_runtime_config_bundle,
        market_capture_root=market_capture_root,
        historical_root=historical_root,
        paper_root=paper_root,
        requests=(entry_request, exit_request),
        event_count=len(captures),
    )

    venue_payload = build_venue_accounting_receipt(
        account_root=route.account_path,
        expected_account_id=ACCOUNT_ID,
        query_start_ms=T0_NS // 1_000_000,
        query_end_ms=T1_NS // 1_000_000,
        observed_ts_ns=T1_NS + 2,
        closed_pnl_rows=[
            {
                "symbol": "BTCUSDT",
                "orderId": "venue-natural-exit",
                "closedPnl": "9.895",
                "openFee": "0.05",
                "closeFee": "0.055",
                "updatedTime": str((exit_event_ts_ns + 6) // 1_000_000),
            }
        ],
        trade_rows=[
            {
                "id": "transaction-natural-entry",
                "tradeId": "execution-natural-entry",
                "type": "TRADE",
                "category": "linear",
                "currency": "USDT",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "qty": "10",
                "tradePrice": "10",
                "orderId": "venue-natural-entry",
                "orderLinkId": entry_command.command_id,
                "cashFlow": "0",
                "funding": "0",
                "fee": "0.05",
                "change": "-0.05",
                "transactionTime": str((T0_NS + 5) // 1_000_000),
            },
            {
                "id": "transaction-natural-exit",
                "tradeId": "execution-natural-exit",
                "type": "TRADE",
                "category": "linear",
                "currency": "USDT",
                "symbol": "BTCUSDT",
                "side": "Sell",
                "qty": "10",
                "tradePrice": "11",
                "orderId": "venue-natural-exit",
                "orderLinkId": exit_command.command_id,
                "cashFlow": "10",
                "funding": "0",
                "fee": "0.055",
                "change": "9.945",
                "transactionTime": str((exit_event_ts_ns + 4) // 1_000_000),
            },
        ],
        settlement_rows=[
            {
                "id": "natural-fixture-settlement",
                "type": "SETTLEMENT",
                "category": "linear",
                "currency": "USDT",
                "symbol": "BTCUSDT",
                "cashFlow": "0",
                "funding": "0.02",
                "fee": "0",
                "change": "0.02",
                "transactionTime": str(funding_ts_ns // 1_000_000),
            }
        ],
        pre_position_rows=[],
        pre_open_order_rows=[],
        post_position_rows=[],
        post_open_order_rows=[],
        requirements=VenueAccountingRequirements(),
    )
    assert venue_payload["venue_accounting_gate_passed"] is True, venue_payload["mismatches"]
    venue_receipt = write_venue_accounting_receipt(
        (tmp_path / "venue" / "accounting.json").resolve(),
        venue_payload,
    )
    return _Fixture(
        long_event_tape=event_paths["long"],
        long_outcome_tape=outcome_paths["long"],
        continuous_event_tape=event_paths["continuous"],
        continuous_outcome_tape=outcome_paths["continuous"],
        target_capture=target_capture,
        account_root=route.account_path,
        safety_capture=safety_capture,
        safety_manifest=safety_manifest,
        freeze_manifest=freeze_manifest,
        effective_runtime_config_bundle=effective_runtime_config_bundle,
        replay_receipt=replay_receipt,
        venue_receipt=venue_receipt,
        batch_ids=(entry_request.batch_id, exit_request.batch_id),
    )


def _build(fixture: _Fixture) -> dict[str, Any]:
    return build_natural_tape_sufficiency_receipt(
        long_event_tape=fixture.long_event_tape,
        long_outcome_tape=fixture.long_outcome_tape,
        continuous_event_tape=fixture.continuous_event_tape,
        continuous_outcome_tape=fixture.continuous_outcome_tape,
        target_capture_path=fixture.target_capture,
        demo_account_root=fixture.account_root,
        safety_target_capture_path=fixture.safety_capture,
        safety_manifest_path=fixture.safety_manifest,
        account_replay_receipt_path=fixture.replay_receipt,
        venue_accounting_receipt_path=fixture.venue_receipt,
        freeze_manifest_path=fixture.freeze_manifest,
        effective_runtime_config_bundle_file=fixture.effective_runtime_config_bundle,
        expected_account_id=ACCOUNT_ID,
        t0_ns=T0_NS,
        t1_ns=T1_NS,
    )


def test_intact_quiet_window_is_inconclusive_and_source_replayable(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    payload = _build(fixture)

    assert payload["integrity_gate_passed"] is True
    assert payload["status"] == "inconclusive"
    assert payload["sufficiency_gate_passed"] is False
    assert payload["sleeve_tapes"]["long"]["covered_hour_count"] == 120
    assert payload["sleeve_tapes"]["continuous"]["covered_hour_count"] == 120
    assert payload["target_capture"]["natural_batch_ids"] == sorted(fixture.batch_ids)
    assert payload["account_lineage"]["filled_command_count"] == 2
    assert payload["execution_authorization"] == "not_granted"

    output = (tmp_path / "evidence" / "natural-sufficiency.json").resolve()
    receipt = write_natural_tape_sufficiency_receipt(output, payload)
    assert receipt.path.stat().st_mode & 0o777 == 0o600
    assert load_natural_tape_sufficiency_receipt(receipt.path) == payload


def test_missing_hour_is_invalid_not_inconclusive(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    rows = fixture.continuous_event_tape.read_bytes().splitlines()
    fixture.continuous_event_tape.write_bytes(b"\n".join(rows[:-1]) + b"\n")
    fixture.continuous_event_tape.chmod(0o600)

    with pytest.raises(ValueError, match="tape|hash|hours|sets differ"):
        _build(fixture)


def test_receipt_loader_rejects_source_mutation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    output = (tmp_path / "evidence" / "natural-sufficiency.json").resolve()
    receipt = write_natural_tape_sufficiency_receipt(output, _build(fixture))
    rows = fixture.long_outcome_tape.read_bytes().splitlines()
    row = json.loads(rows[0])
    row["outcome"]["decision_keys"] = ["invented"]
    fixture.long_outcome_tape.write_bytes(canonical_json(row) + b"\n" + b"\n".join(rows[1:]) + b"\n")
    fixture.long_outcome_tape.chmod(0o600)

    with pytest.raises(ValueError, match="hash|differs|changed"):
        load_natural_tape_sufficiency_receipt(receipt.path)


def test_receipt_loader_rejects_freeze_manifest_mutation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    output = (tmp_path / "evidence" / "natural-sufficiency.json").resolve()
    receipt = write_natural_tape_sufficiency_receipt(output, _build(fixture))
    freeze = json.loads(fixture.freeze_manifest.read_text(encoding="utf-8"))
    freeze["freeze_id"] = "substituted-freeze"
    fixture.freeze_manifest.write_bytes(canonical_json(freeze) + b"\n")
    fixture.freeze_manifest.chmod(0o600)

    with pytest.raises(ValueError, match="freeze|hash|differs|changed"):
        load_natural_tape_sufficiency_receipt(receipt.path)


def test_t0_must_be_hour_aligned(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(ValueError, match="hour boundary"):
        build_natural_tape_sufficiency_receipt(
            long_event_tape=fixture.long_event_tape,
            long_outcome_tape=fixture.long_outcome_tape,
            continuous_event_tape=fixture.continuous_event_tape,
            continuous_outcome_tape=fixture.continuous_outcome_tape,
            target_capture_path=fixture.target_capture,
            demo_account_root=fixture.account_root,
            safety_target_capture_path=fixture.safety_capture,
            safety_manifest_path=fixture.safety_manifest,
            account_replay_receipt_path=fixture.replay_receipt,
            venue_accounting_receipt_path=fixture.venue_receipt,
            freeze_manifest_path=fixture.freeze_manifest,
            effective_runtime_config_bundle_file=fixture.effective_runtime_config_bundle,
            expected_account_id=ACCOUNT_ID,
            t0_ns=T0_NS + 1,
            t1_ns=T1_NS + 1,
        )
