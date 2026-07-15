from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import liquidity_migration.captured_account_replay as replay_module
import liquidity_migration.kernel_parity as kernel_module
import liquidity_migration.strategy_event_parity as event_module

from liquidity_migration.account_kernel import (
    AccountEvent,
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    account_transactions_path,
    read_account_journal,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.execution_twin_calibration import CalibrationRequirements
from liquidity_migration.kernel_parity import (
    KERNEL_PARITY_CONTRACT_ID,
    KERNEL_PARITY_SCHEMA_VERSION,
    QUANTITY_ABS_TOLERANCE,
    build_comparison_scope,
    build_kernel_parity_receipt,
    compare_kernel_journals,
    load_kernel_parity_receipt,
    main as kernel_parity_main,
    verify_kernel_parity_receipt,
    write_comparison_scope,
    write_kernel_parity_receipt,
)


@pytest.fixture(autouse=True)
def _load_test_effective_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    def load_bundle(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
        resolved = Path(path).resolve(strict=True)
        binding = json.loads(resolved.read_text(encoding="utf-8"))
        binding["path"] = str(resolved)
        binding["file_sha256"] = _sha256(resolved.read_bytes())
        return {}, binding

    monkeypatch.setattr(
        kernel_module,
        "load_effective_runtime_config_bundle_binding",
        load_bundle,
    )
    monkeypatch.setattr(
        replay_module,
        "load_captured_account_replay_receipt",
        lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
    )
    monkeypatch.setattr(
        event_module,
        "load_strategy_event_parity_receipt",
        lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _market(*, key: str) -> MarketInputRef:
    return MarketInputRef(
        input_key=key,
        symbol="BUSDT",
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        reference_price=10.0,
        bid_price=9.9,
        ask_price=10.1,
        book_sequence=42,
        source="captured_bybit_l2",
    )


def _target(*, batch_id: str, qty: float) -> DesiredTarget:
    return DesiredTarget(
        decision_key=f"decision:{batch_id}",
        target_key="continuous/main/BUSDT",
        sleeve="continuous",
        strategy_id="continuous-v1",
        component_id="main",
        symbol="BUSDT",
        signed_qty=qty,
        reference_price=10.0,
        leverage=10.0,
        reason=f"target:{batch_id}",
    )


def _rules() -> dict[str, InstrumentRules]:
    return {
        "BUSDT": InstrumentRules(
            symbol="BUSDT",
            qty_step=0.1,
            min_qty=0.1,
            min_notional=1.0,
            max_order_qty=100.0,
            max_leverage=20.0,
        )
    }


def _policy() -> AccountRiskPolicy:
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=1_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=1_000.0,
        max_leverage=10.0,
    )


def _snapshot(*, key: str) -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        equity_usdt=10_000.0,
        available_margin_usdt=9_000.0,
        snapshot_key=key,
        snapshot_ts_ns=950_000_000,
    )


def _kernel(root: Path, *, account_id: str) -> AccountExecutionKernel:
    return AccountExecutionKernel(
        root,
        account_id=account_id,
        clock=VirtualClock(
            current_wall_ns=1_100_000_000,
            current_monotonic_ns=100_000_000,
        ),
        id_seed="kernel-parity-v2-test",
    )


def _submit(
    root: Path,
    *,
    account_id: str,
    batch_id: str,
    qty: float = -2.0,
) -> tuple[AccountExecutionKernel, str | None]:
    kernel = _kernel(root, account_id=account_id)
    result = kernel.submit_targets(
        batch_id=batch_id,
        market_inputs=[_market(key=f"book:{batch_id}")],
        targets=[_target(batch_id=batch_id, qty=qty)],
        risk_snapshot=_snapshot(key=f"wallet:{batch_id}"),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    command_id = result.commands[0].command_id if result.commands else None
    return kernel, command_id


def _record_single_fill(
    kernel: AccountExecutionKernel,
    command_id: str,
    *,
    suffix: str,
    price: float = 10.1,
    fee_usdt: float = 0.01,
) -> None:
    kernel.record_ack(
        command_id=command_id,
        accepted=True,
        venue_order_id=f"venue:{suffix}",
        exchange_ts_ns=1_200_000_000,
        local_ack_ts_ns=1_210_000_000,
    )
    kernel.record_fill(
        command_id=command_id,
        execution_id=f"execution:{suffix}",
        signed_qty=-2.0,
        price=price,
        fee_usdt=fee_usdt,
        exchange_ts_ns=1_220_000_000,
        local_receive_ts_ns=1_225_000_000,
        metadata={"fee_status": "modeled_execution_fee"},
    )


def _natural_roots(
    tmp_path: Path,
    *,
    batch_id: str = "natural-1",
    demo_prefix: bool = False,
) -> dict[str, Path]:
    roots = {environment: tmp_path / environment for environment in ("historical", "paper", "demo")}
    if demo_prefix:
        _submit(
            roots["demo"],
            account_id="demo-account",
            batch_id="v7-prefix-outside-natural-scope",
            qty=0.0,
        )
    command_ids: dict[str, str] = {}
    for environment, root in roots.items():
        kernel, command_id = _submit(
            root,
            account_id=f"{environment}-account",
            batch_id=batch_id,
        )
        assert command_id is not None
        command_ids[environment] = command_id
        if environment in {"historical", "paper"}:
            _record_single_fill(kernel, command_id, suffix="modeled")
        else:
            kernel.record_ack(
                command_id=command_id,
                accepted=True,
                venue_order_id="bybit-demo-order",
                exchange_ts_ns=1_201_000_000,
                local_ack_ts_ns=1_219_000_000,
                metadata={"source": "actual_bybit_demo"},
            )
            kernel.record_fill(
                command_id=command_id,
                execution_id="bybit-demo-fill-1",
                signed_qty=-0.7,
                price=10.15,
                fee_usdt=0.004,
                exchange_ts_ns=1_225_000_000,
                local_receive_ts_ns=1_240_000_000,
                metadata={"fee_status": "observed_execution_fee"},
            )
            kernel.record_fill(
                command_id=command_id,
                execution_id="bybit-demo-fill-2",
                signed_qty=-1.3,
                price=10.25,
                fee_usdt=0.009,
                exchange_ts_ns=1_235_000_000,
                local_receive_ts_ns=1_250_000_000,
                metadata={"fee_status": "observed_execution_fee"},
            )
    assert len(set(command_ids.values())) == 3
    return roots


def _replace_target_quantity(
    events: Sequence[AccountEvent],
    *,
    value: Any,
) -> tuple[AccountEvent, ...]:
    output: list[AccountEvent] = []
    for event in events:
        if event.event_type == AccountEventType.TARGET.value:
            output.append(replace(event, payload={**event.payload, "signed_qty": value}))
        else:
            output.append(event)
    return tuple(output)


def _git_repo(root: Path) -> str:
    root.mkdir(parents=True)
    (root / "marker.txt").write_text("kernel parity contract\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "marker.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Kernel Parity Test",
            "-c",
            "user.email=kernel-parity@example.invalid",
            "commit",
            "-q",
            "-m",
            "test fixture",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _journal_stream_sha256(events: Sequence[AccountEvent]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event.to_dict()))
        digest.update(b"\n")
    return digest.hexdigest()


def _calibration_receipt(
    path: Path,
    *,
    calibration_root: Path,
    expected_account_id: str,
) -> Path:
    events = read_account_journal(calibration_root, verify=True)
    capture_manifest = [{"path": "BUSDT/segment-000001.jsonl", "sha256": _sha256(b"capture\n")}]
    requirements = CalibrationRequirements()
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
    receipt: dict[str, Any] = {
        "schema_version": 3,
        "kind": "bybit_demo_market_order_execution_twin_calibration",
        "observed_ts_ns": 1_050_000_000,
        "expected_account_id": expected_account_id,
        "inputs": {
            "account_root": str(calibration_root),
            "account_journal_sha256": _journal_stream_sha256(events),
            "account_last_event_hash": events[-1].event_hash,
            "market_capture_root": str(calibration_root / "recycled-live-capture-path"),
            "market_capture_manifest": capture_manifest,
            "market_capture_manifest_sha256": _sha256(canonical_json({"files": capture_manifest})),
            "local_minus_exchange_ns": 0,
            "clock_offset_receipt_sha256": "c" * 64,
        },
        "requirements": asdict(requirements),
        "sample_counts": {
            "feed_latency": 5_000,
            "target_events": 30,
            "order_commands": 30,
            "request_ack_rtt": 30,
            "filled_orders": 30,
            "submit_to_first_fill": 30,
            "submit_to_first_fill_orders": 30,
            "fill_response": 30,
            "fill_response_orders": 30,
            "pnl_events": 10,
            "symbols": 3,
            "slippage_orders": 30,
        },
        "latency_ns": {
            "order_entry_clock_adjusted": {"count": 30},
            "order_response_clock_adjusted": {"count": 30},
            "submit_to_first_fill_clock_adjusted": {"count": 30},
            "fill_response_clock_adjusted": {"count": 30},
            "partial_fill_spacing": {"count": 3},
        },
        "fills": {
            "multi_fill_orders": 3,
            "partial_fill_calibrated": True,
            "allow_partial_fills": True,
            "book_level_partition_calibrated": False,
            "calibration_scope": (
                "observed multifill/incomplete occurrence, fill ratio, and positive "
                "within-order venue-timestamp spacing only"
            ),
            "uncalibrated_behavior": "single_level_full_fill_or_reject",
        },
        "slippage": {"fee_bps": 5.5},
        "queue_assumption": {"passive_queue_calibrated": False},
        "context_link_ratio": 0.95,
        "reference_match_ratio": 0.99,
        "negative_adjusted_feed_latency_ratio": 0.0,
        "negative_adjusted_order_entry_latency_ratio": 0.0,
        "negative_adjusted_order_response_latency_ratio": 0.0,
        "negative_adjusted_submit_to_first_fill_latency_ratio": 0.0,
        "negative_adjusted_fill_response_latency_ratio": 0.0,
        "sample_gate": sample_gate,
        "market_order_smoke_gate_passed": True,
        "partial_fill_gate": {
            "observed_multi_fill_orders": True,
            "partial_fill_spacing_samples": True,
        },
        "partial_fill_calibration_gate_passed": True,
        "execution_twin_gate_passed": True,
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = _sha256(canonical_json(receipt))
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    return path


def _receipt_inputs(
    tmp_path: Path,
    roots: Mapping[str, Path],
    *,
    batch_id: str,
) -> tuple[dict[str, Path], Path, str]:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    evidence = {
        "captured_account_replay_receipt": evidence_root / "captured-account-replay.json",
        "event_parity_receipt": evidence_root / "event-parity.json",
        "fresh_epoch_reset_receipt": evidence_root / "reset.sha256",
        "risk_policy_file": evidence_root / "risk.json",
        "rules_file": evidence_root / "rules.json",
    }
    for name, path in evidence.items():
        if name in {"captured_account_replay_receipt", "event_parity_receipt"}:
            continue
        path.write_text(json.dumps({"kind": name}, sort_keys=True) + "\n")

    calibration_root = tmp_path / "calibration-pre-reset"
    _submit(
        calibration_root,
        account_id="demo-account",
        batch_id="v7-calibration-only",
        qty=-1.0,
    )
    calibration = _calibration_receipt(
        evidence_root / "twin-calibration.json",
        calibration_root=calibration_root,
        expected_account_id="demo-account",
    )
    evidence["twin_calibration_receipt"] = calibration

    repo = tmp_path / "clean-repo"
    commit = _git_repo(repo)
    effective_bundle = evidence_root / "effective-runtime-config-bundle.json"
    effective_bundle.write_text(
        json.dumps(
            {
                "artifact_sha256": _sha256(b"effective-runtime-config-bundle"),
                "validator": "natural_effective_runtime_config_bundle_v2",
                "created_ts_ns": 1,
                "repository": {
                    "root": str(repo.resolve()),
                    "candidate_commit": commit,
                    "origin_main_commit": "b" * 40,
                },
                "freeze": {
                    "path": str((evidence_root / "freeze.json").resolve()),
                    "file_sha256": _sha256(b"freeze-file"),
                    "artifact_sha256": _sha256(b"freeze-artifact"),
                    "freeze_id": "kernel-parity-fixture",
                },
                "natural_run_config": {
                    "path": str((evidence_root / "run-config.json").resolve()),
                    "file_sha256": _sha256(b"run-config-file"),
                    "artifact_sha256": _sha256(b"run-config-artifact"),
                },
                "candidate_universe": {
                    "path": str((evidence_root / "candidate.json").resolve()),
                    "file_sha256": _sha256(b"candidate-file"),
                    "artifact_sha256": _sha256(b"candidate-artifact"),
                },
                "window": {
                    "t0_ns": 1,
                    "t1_ns": 2,
                    "interval": "half_open_[t0,t1)",
                },
                "runtime_paths": {
                    "target_capture_path": str((evidence_root / "targets.jsonl").resolve()),
                    "sleeves": {},
                },
                "receipts": {},
                "execution_authorization": "not_granted",
            },
            sort_keys=True,
        )
        + "\n"
    )
    effective_bundle.chmod(0o600)
    evidence["effective_runtime_config_bundle_file"] = effective_bundle
    effective_runtime_config = kernel_module.load_effective_runtime_config_bundle_binding(
        effective_bundle
    )[1]

    target_capture = evidence_root / "targets.jsonl"
    target_capture.write_text('{"target":true}\n', encoding="utf-8")
    target_capture.chmod(0o600)
    target_stat = target_capture.stat()
    target_identity = {
        "path": str(target_capture.resolve()),
        "size": target_stat.st_size,
        "sha256": _sha256(target_capture.read_bytes()),
        "device": target_stat.st_dev,
        "inode": target_stat.st_ino,
        "mtime_ns": target_stat.st_mtime_ns,
        "mode": target_stat.st_mode & 0o777,
    }
    captured_payload = {
        "has_durable_request_batches": True,
        "ordered_batch_ids": [batch_id],
        "effective_runtime_config": effective_runtime_config,
        "source_files": {"target_scheduling_capture": target_identity},
        "outputs": {
            "historical_root": str(roots["historical"].resolve()),
            "paper_root": str(roots["paper"].resolve()),
            "historical_account_journal_sha256": _journal_stream_sha256(
                read_account_journal(roots["historical"], verify=True)
            ),
            "paper_account_journal_sha256": _journal_stream_sha256(
                read_account_journal(roots["paper"], verify=True)
            ),
        },
    }
    evidence["captured_account_replay_receipt"].write_text(
        json.dumps(captured_payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    evidence["captured_account_replay_receipt"].chmod(0o400)
    event_payload = {
        "strategy_event_replay_gate_passed": True,
        "replay_provenance": {
            "deployment_valid": True,
            "replay_manifest": {
                "path": str((evidence_root / "replay-manifest.json").resolve()),
                "size_bytes": 1,
                "sha256": _sha256(b"replay-manifest"),
                "schema_version": 2,
                "artifact_sha256": _sha256(b"replay-manifest-artifact"),
                "created_ts_ns": 1,
            },
            "canonical_source_capture": {
                "path": target_identity["path"],
                "size_bytes": target_identity["size"],
                "sha256": target_identity["sha256"],
                "device": target_identity["device"],
                "inode": target_identity["inode"],
                "mtime_ns": target_identity["mtime_ns"],
                "mode": target_identity["mode"],
                "uid": target_stat.st_uid,
                "nlink": target_stat.st_nlink,
                "capture_event_count": 1,
                "capture_chain_hash": _sha256(b"capture-chain"),
                "source_environment": "demo",
            },
        }
    }
    evidence["event_parity_receipt"].write_text(
        json.dumps(event_payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    evidence["event_parity_receipt"].chmod(0o600)

    scope = evidence_root / "comparison-scope.json"
    scope_payload = build_comparison_scope(
        captured_account_replay_receipt=evidence["captured_account_replay_receipt"],
        event_parity_receipt=evidence["event_parity_receipt"],
    )
    write_comparison_scope(scope.resolve(), scope_payload)
    assert roots["demo"] != calibration_root
    return evidence, scope, commit


def _build_receipt(
    tmp_path: Path,
    roots: Mapping[str, Path],
    *,
    batch_id: str,
) -> tuple[dict[str, Any], dict[str, Path], Path, Path, str]:
    evidence, scope, commit = _receipt_inputs(tmp_path, roots, batch_id=batch_id)
    repo = tmp_path / "clean-repo"
    receipt = build_kernel_parity_receipt(
        roots,
        comparison_scope_file=scope,
        event_parity_receipt=evidence["event_parity_receipt"],
        fresh_epoch_reset_receipt=evidence["fresh_epoch_reset_receipt"],
        risk_policy_file=evidence["risk_policy_file"],
        rules_file=evidence["rules_file"],
        effective_runtime_config_bundle_file=evidence[
            "effective_runtime_config_bundle_file"
        ],
        twin_calibration_receipt=evidence["twin_calibration_receipt"],
        repo_root=repo,
        expected_commit=commit,
        quantity_tolerance=QUANTITY_ABS_TOLERANCE,
    )
    return receipt, evidence, scope, repo, commit


def test_actual_demo_outcomes_are_classified_not_misclaimed_as_exact(tmp_path: Path) -> None:
    roots = _natural_roots(tmp_path)
    report = compare_kernel_journals(
        roots,
        comparison_batch_ids=["natural-1"],
        quantity_tolerance=QUANTITY_ABS_TOLERANCE,
    )

    assert report.passed
    assert report.command_id_mapping_one_to_one
    assert report.historical_paper_normalized_modeled_execution_exact
    assert len(report.command_id_mapping) == 1
    raw_ids = report.command_id_mapping[0]["environment_command_ids"]
    assert len(set(raw_ids.values())) == 3


def test_demo_prefix_is_allowed_but_interleaved_normal_batch_is_refused(tmp_path: Path) -> None:
    roots = {environment: tmp_path / environment for environment in ("historical", "paper", "demo")}
    _submit(roots["demo"], account_id="demo", batch_id="v7-prefix", qty=0.0)
    for environment, root in roots.items():
        _submit(root, account_id=environment, batch_id="natural-a", qty=-1.0)
    assert compare_kernel_journals(roots, comparison_batch_ids=["natural-a"]).passed

    interleaved = {environment: tmp_path / f"interleaved-{environment}" for environment in roots}
    for environment, root in interleaved.items():
        _submit(root, account_id=environment, batch_id="natural-a", qty=-1.0)
        if environment == "demo":
            _submit(root, account_id=environment, batch_id="unscoped-normal", qty=-1.5)
        _submit(root, account_id=environment, batch_id="natural-b", qty=-2.0)
    with pytest.raises(ValueError, match="unscoped strategy batches: unscoped-normal"):
        compare_kernel_journals(
            interleaved,
            comparison_batch_ids=["natural-a", "natural-b"],
        )


def test_scope_and_quantities_fail_closed(tmp_path: Path) -> None:
    roots = _natural_roots(tmp_path)
    with pytest.raises(ValueError, match="journal order"):
        compare_kernel_journals(
            roots,
            comparison_batch_ids=["natural-1", "missing-batch"],
        )
    with pytest.raises(ValueError, match="fixed prospectively"):
        compare_kernel_journals(
            roots,
            comparison_batch_ids=["natural-1"],
            quantity_tolerance=1e-9,
        )

    malformed_demo = _replace_target_quantity(
        read_account_journal(roots["demo"], verify=True),
        value=None,
    )
    with pytest.raises(ValueError, match="must be present and finite"):
        compare_kernel_journals(
            {
                "historical": roots["historical"],
                "paper": roots["paper"],
                "demo": malformed_demo,
            },
            comparison_batch_ids=["natural-1"],
        )


def test_current_receipt_reopens_sources_and_rejects_rehashed_forgery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    roots = _natural_roots(tmp_path, demo_prefix=True)
    receipt, evidence, scope, repo, commit = _build_receipt(
        tmp_path,
        roots,
        batch_id="natural-1",
    )
    assert receipt["schema_version"] == KERNEL_PARITY_SCHEMA_VERSION
    assert receipt["contract_id"] == KERNEL_PARITY_CONTRACT_ID
    assert receipt["journal_parity_passed"] is True
    assert receipt["full_cross_environment_acceptance_passed"] is False
    assert receipt["report"]["historical_paper_normalized_modeled_execution_exact"] is True
    assert receipt["epoch_bindings"]["calibration_pre_reset"]["embedded_source_paths_dereferenced"] is False
    demo_prefix = receipt["sources"]["demo"]["demo_prefix_classification"]
    assert demo_prefix["event_count"] > 0
    assert demo_prefix["primary_family_counts"]["out_of_scope_strategy_plan"] > 0
    assert receipt["sources"]["demo"]["classified_event_counts"]["comparison_scope"][
        "fee_pnl_funding_fact_counts"
    ]["fee_observed"] == 2
    assert verify_kernel_parity_receipt(receipt) == receipt

    output = tmp_path / "kernel-parity-v2.json"
    write_kernel_parity_receipt(output, receipt)
    assert load_kernel_parity_receipt(output) == receipt
    assert os.stat(output).st_mode & 0o077 == 0
    preserved = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_kernel_parity_receipt(output, receipt)
    assert output.read_bytes() == preserved

    cli_output = tmp_path / "kernel-parity-v2-cli.json"
    environment_args = [
        item
        for environment, root in roots.items()
        for item in ("--environment", f"{environment}={root}")
    ]
    result = kernel_parity_main(
        [
            *environment_args,
            "--comparison-scope-file",
            str(scope),
            "--event-parity-receipt",
            str(evidence["event_parity_receipt"]),
            "--fresh-epoch-reset-receipt",
            str(evidence["fresh_epoch_reset_receipt"]),
            "--risk-policy-file",
            str(evidence["risk_policy_file"]),
            "--rules-file",
            str(evidence["rules_file"]),
            "--effective-runtime-config-bundle",
            str(evidence["effective_runtime_config_bundle_file"]),
            "--twin-calibration-receipt",
            str(evidence["twin_calibration_receipt"]),
            "--repo-root",
            str(repo),
            "--expected-commit",
            commit,
            "--output",
            str(cli_output),
        ]
    )
    assert result == 0
    cli_receipt = json.loads(capsys.readouterr().out)
    assert load_kernel_parity_receipt(cli_output) == cli_receipt
    assert cli_receipt["created_ts_ns"] >= receipt["created_ts_ns"]
    for field in set(receipt) - {"created_ts_ns", "artifact_sha256"}:
        assert cli_receipt[field] == receipt[field]

    forged = json.loads(json.dumps(receipt))
    forged["sources"]["demo"]["root"] = str(tmp_path / "does-not-exist")
    forged["artifact_sha256"] = ""
    forged["artifact_sha256"] = _sha256(canonical_json(forged))
    with pytest.raises(ValueError, match="account root is missing"):
        verify_kernel_parity_receipt(forged)

    transaction = next(account_transactions_path(roots["demo"]).glob("*.json"))
    transaction.write_bytes(transaction.read_bytes() + b" \n")
    with pytest.raises(ValueError, match="does not match recomputed sources"):
        verify_kernel_parity_receipt(receipt)


def test_receipt_rejects_calibration_natural_epoch_hash_reuse(tmp_path: Path) -> None:
    roots = _natural_roots(tmp_path)
    evidence, scope, commit = _receipt_inputs(tmp_path, roots, batch_id="natural-1")
    calibration = json.loads(evidence["twin_calibration_receipt"].read_text())
    demo_events = read_account_journal(roots["demo"], verify=True)
    calibration["inputs"]["account_journal_sha256"] = _journal_stream_sha256(demo_events)
    calibration["inputs"]["account_last_event_hash"] = demo_events[-1].event_hash
    calibration["artifact_sha256"] = ""
    calibration["artifact_sha256"] = _sha256(canonical_json(calibration))
    evidence["twin_calibration_receipt"].write_text(json.dumps(calibration) + "\n")

    with pytest.raises(ValueError, match="reuse the same journal hash"):
        build_kernel_parity_receipt(
            roots,
            comparison_scope_file=scope,
            event_parity_receipt=evidence["event_parity_receipt"],
            fresh_epoch_reset_receipt=evidence["fresh_epoch_reset_receipt"],
            risk_policy_file=evidence["risk_policy_file"],
            rules_file=evidence["rules_file"],
            effective_runtime_config_bundle_file=evidence[
                "effective_runtime_config_bundle_file"
            ],
            twin_calibration_receipt=evidence["twin_calibration_receipt"],
            repo_root=tmp_path / "clean-repo",
            expected_commit=commit,
        )


def test_receipt_rejects_calibration_that_weakens_registered_floors(
    tmp_path: Path,
) -> None:
    roots = _natural_roots(tmp_path)
    evidence, scope, commit = _receipt_inputs(tmp_path, roots, batch_id="natural-1")
    calibration = json.loads(evidence["twin_calibration_receipt"].read_text())
    calibration["requirements"]["min_feed_samples"] = 0
    calibration["artifact_sha256"] = ""
    calibration["artifact_sha256"] = _sha256(canonical_json(calibration))
    evidence["twin_calibration_receipt"].write_text(json.dumps(calibration) + "\n")

    with pytest.raises(ValueError, match="weaken registered floors"):
        build_kernel_parity_receipt(
            roots,
            comparison_scope_file=scope,
            event_parity_receipt=evidence["event_parity_receipt"],
            fresh_epoch_reset_receipt=evidence["fresh_epoch_reset_receipt"],
            risk_policy_file=evidence["risk_policy_file"],
            rules_file=evidence["rules_file"],
            effective_runtime_config_bundle_file=evidence[
                "effective_runtime_config_bundle_file"
            ],
            twin_calibration_receipt=evidence["twin_calibration_receipt"],
            repo_root=tmp_path / "clean-repo",
            expected_commit=commit,
        )


def test_receipt_rejects_identical_journal_outside_captured_replay_root(
    tmp_path: Path,
) -> None:
    roots = _natural_roots(tmp_path)
    evidence, scope, commit = _receipt_inputs(tmp_path, roots, batch_id="natural-1")
    copied_historical = tmp_path / "copied-historical"
    shutil.copytree(roots["historical"], copied_historical)
    substituted = {**roots, "historical": copied_historical}

    with pytest.raises(
        ValueError,
        match="kernel historical root is not the captured-account replay output",
    ):
        build_kernel_parity_receipt(
            substituted,
            comparison_scope_file=scope,
            event_parity_receipt=evidence["event_parity_receipt"],
            fresh_epoch_reset_receipt=evidence["fresh_epoch_reset_receipt"],
            risk_policy_file=evidence["risk_policy_file"],
            rules_file=evidence["rules_file"],
            effective_runtime_config_bundle_file=evidence[
                "effective_runtime_config_bundle_file"
            ],
            twin_calibration_receipt=evidence["twin_calibration_receipt"],
            repo_root=tmp_path / "clean-repo",
            expected_commit=commit,
        )
