from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import liquidity_migration.account_cutover_authority as authority
import liquidity_migration.fresh_deploy_epoch as fresh_deploy_epoch
import liquidity_migration.natural_tape_sufficiency as natural_sufficiency
import liquidity_migration.strategy_event_parity as strategy_event_parity


COMMIT = "a" * 40
ORIGIN_MAIN = "d" * 40
NOW_NS = 2_000_000_000_000
T0_NS = 3_600_000_000_000
T1_NS = T0_NS + 120 * 60 * 60 * 1_000_000_000
FREEZE_ID = "natural-cutover-v1"
DEMO_ACCOUNT_ID = "bybit-demo-unified"
DEMO_ACCOUNT_ROOT = "/evidence/demo-account"
DEMO_CAPTURE_ROOT = "/evidence/demo-capture"
HISTORICAL_ROOT = "/evidence/replay/historical"
PAPER_ROOT = "/evidence/replay/paper"
STOPPED_SEAL_PATH = "/evidence/stopped-natural-epoch.json"

OLD_ROOT_PATHS = [
    DEMO_ACCOUNT_ROOT,
    "/evidence/demo-inbox",
    DEMO_CAPTURE_ROOT,
    "/evidence/paper-account",
    "/evidence/paper-inbox",
    "/evidence/paper-capture",
    "/evidence/long-demo",
    "/evidence/long-paper",
    "/evidence/continuous-demo",
    "/evidence/continuous-paper",
    "/evidence/natural-evidence",
]


def test_cutover_artifact_writer_refuses_to_replace_preserved_evidence(
    tmp_path: Path,
) -> None:
    output = tmp_path / "authority.json"
    output.write_bytes(b"preserved failed attempt\n")
    output.chmod(0o600)

    with pytest.raises(FileExistsError):
        authority._atomic_write(output, {"replacement": True})  # noqa: SLF001

    assert output.read_bytes() == b"preserved failed attempt\n"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _artifact(role: str) -> str:
    if role in authority._VENUE_RECEIPT_ROLES:
        return _digest("venue-accounting-receipt")
    return _digest(role)


def _file_digest(role: str) -> str:
    if role in authority._VENUE_RECEIPT_ROLES:
        return _digest("venue-accounting-file")
    return _digest(f"{role}:file")


def _batch_hash() -> str:
    return authority._sequence_sha256(["batch-a", "batch-b"])


def _safety_hash() -> str:
    return authority._sequence_sha256([f"natural-safety-flatten/{FREEZE_ID}/1"])


def _captured_replay_outputs() -> dict[str, Any]:
    return {
        "historical_root": HISTORICAL_ROOT,
        "paper_root": PAPER_ROOT,
        "historical_account_journal_sha256": _digest("historical-journal"),
        "paper_account_journal_sha256": _digest("paper-journal"),
        "historical_final_state_hash": _digest("historical-state"),
        "paper_final_state_hash": _digest("paper-state"),
        "historical_strategy_event_tape_hash": _digest("historical-event-tape"),
        "paper_strategy_event_tape_hash": _digest("paper-event-tape"),
        "historical_batch_summaries": [{"batch_id": "batch-a"}],
        "paper_batch_summaries": [{"batch_id": "batch-a"}],
    }


def _event_replay_provenance() -> dict[str, Any]:
    return {
        "deployment_valid": True,
        "replay_manifest": {
            "path": "/derived-target-replay/replay_manifest.json",
            "size_bytes": 1_024,
            "sha256": _digest("target-replay-manifest-file"),
            "schema_version": 2,
            "artifact_sha256": _digest("target-replay-manifest-artifact"),
            "created_ts_ns": NOW_NS + 15,
        },
        "canonical_source_capture": {
            "path": "/evidence/natural-evidence/target-capture.jsonl",
            "size_bytes": 2_048,
            "sha256": _digest("target-capture-bytes"),
            "device": 7,
            "inode": 30_000,
            "mtime_ns": NOW_NS,
            "mode": 0o400,
            "uid": 501,
            "nlink": 1,
            "capture_event_count": 2,
            "capture_chain_hash": _digest("target-capture"),
            "source_environment": "demo",
        },
    }


def _target_replay_output_files() -> list[dict[str, str]]:
    return [
        {
            "path": f"/derived-target-replay/{environment}/{filename}",
            "sha256": _digest(f"target-replay:{environment}:{filename}"),
        }
        for environment in ("historical", "paper", "demo")
        for filename in (
            "strategy_event_tape.jsonl",
            "strategy_event_decision_tape.jsonl",
            "scheduled_target_requests.jsonl",
            "replay_input.jsonl",
        )
    ]


def _path_identity(path: str, index: int) -> dict[str, Any]:
    return {
        "path": path,
        "kind": "directory",
        "device": 7,
        "inode": 10_000 + index,
        "mode": 0o700,
        "uid": 501,
    }


def _old_path_identities() -> list[dict[str, Any]]:
    return [_path_identity(path, index) for index, path in enumerate(OLD_ROOT_PATHS)]


def _stopped_seal_identity() -> dict[str, Any]:
    return {
        "path": STOPPED_SEAL_PATH,
        "size_bytes": 1234,
        "sha256": _file_digest("stopped_natural_epoch"),
        "device": 7,
        "inode": 20_000,
        "mtime_ns": NOW_NS + 1,
        "mode": 0o600,
        "uid": 501,
    }


def _fresh_root_identities() -> dict[str, dict[str, Any]]:
    roles = (
        "demo_account",
        "demo_inbox",
        "demo_capture",
        "paper_account",
        "paper_inbox",
        "paper_capture",
        "long_demo",
        "long_paper",
        "continuous_demo",
        "continuous_paper",
    )
    return {role: _path_identity(f"/fresh/{role.replace('_', '-')}", 100 + index) for index, role in enumerate(roles)}


def _effective_config_bindings() -> dict[str, Any]:
    return {
        "effective_runtime_config_bundle_file_sha256": _digest("effective-config-bundle-file"),
        "effective_runtime_config_bundle_artifact_sha256": _digest("effective-config-bundle-artifact"),
        "effective_runtime_config_validator": "natural_effective_runtime_config_bundle_v2",
        "effective_runtime_config_freeze_path": "/evidence/natural-freeze.json",
        "effective_runtime_config_freeze_file_sha256": _file_digest("natural_cutover_freeze_manifest"),
        "effective_runtime_config_freeze_artifact_sha256": _artifact("natural_cutover_freeze_manifest"),
        "effective_runtime_config_freeze_id": FREEZE_ID,
        "effective_runtime_config_run_config_path": "/evidence/natural-run-config.json",
        "effective_runtime_config_run_config_file_sha256": _digest("natural-run-config-file"),
        "effective_runtime_config_run_config_artifact_sha256": _digest("natural-run-config-artifact"),
        "effective_runtime_config_candidate_path": "/evidence/candidate-universe.json",
        "effective_runtime_config_candidate_file_sha256": _digest("candidate-universe-file"),
        "effective_runtime_config_candidate_artifact_sha256": _digest("candidate-universe"),
        "effective_runtime_config_t0_ns": T0_NS,
        "effective_runtime_config_t1_ns": T1_NS,
        "effective_runtime_config_candidate_commit": COMMIT,
        "effective_runtime_config_origin_main_commit": ORIGIN_MAIN,
        "effective_runtime_config_target_capture_path": "/evidence/target-capture.jsonl",
    }


def _machine_id(tmp_path: Path, value: str = "machine-a") -> Path:
    path = tmp_path / f"{value}.machine-id"
    path.write_text(value + "\n", encoding="utf-8")
    return path


def _passing_assessment(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    assessment = authority.assessment_template(
        authorized_commit=COMMIT,
        authorized_by="cutover-owner",
    )
    for gate in assessment["gates"].values():
        gate["status"] = "passed"
        gate["decision"] = "Reviewed against the registered claim and accepted."
    shared_venue_path = tmp_path / "venue-accounting.json"
    for evidence_id, evidence in assessment["evidence"].items():
        role = evidence["role"]
        path = shared_venue_path if role in authority._VENUE_RECEIPT_ROLES else tmp_path / f"{evidence_id}.json"
        if not path.exists():
            path.write_text(
                json.dumps({"evidence_id": evidence_id, "observed": True}) + "\n",
                encoding="utf-8",
            )
        evidence["path"] = str(path)
        evidence["claim"] = f"Evidence scoped to {role}."
    path = tmp_path / "assessment.json"
    path.write_text(json.dumps(assessment, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return assessment, path


def _fake_bindings(
    role: str,
    *,
    evidence_root: str = "/derived-evidence",
) -> dict[str, Any]:
    common = {"receipt_file_sha256": _file_digest(role)}
    completion_ts_ns = {
        "captured_account_replay": NOW_NS + 20,
        "execution_twin_drift": NOW_NS + 25,
        "event_clock_comparison": NOW_NS + 30,
        "kernel_parity": NOW_NS + 40,
        "natural_tape_sufficiency": NOW_NS + 50,
    }.get(role, NOW_NS + 50)
    post_seal = {
        "declared_analysis_completed_ts_ns": completion_ts_ns,
        "analysis_source_files": [
            {
                "path": "/evidence/natural-evidence/target-capture.jsonl",
                "sha256": _digest("target-capture-bytes"),
            }
        ],
        "analysis_source_roots": [],
    }
    if role == "natural_cutover_freeze_manifest":
        return {
            **common,
            "freeze_id": FREEZE_ID,
            "freeze_manifest_path": "/evidence/natural-freeze.json",
            "created_ts_ns": NOW_NS - 1,
            "authorized_commit": COMMIT,
            "origin_main_commit": ORIGIN_MAIN,
            "local_suite_artifact_sha256": _digest("local-suite"),
            "linux_ci_artifact_sha256": _digest("linux-ci"),
            "t0_ns": T0_NS,
            "t1_ns": T1_NS,
            "account_ids": {"demo": DEMO_ACCOUNT_ID, "paper": "bybit-paper-unified"},
            "roots": {
                "demo": {
                    "account": DEMO_ACCOUNT_ROOT,
                    "capture": DEMO_CAPTURE_ROOT,
                    "inbox": "/evidence/demo-inbox",
                },
                "paper": {
                    "account": "/evidence/paper-account",
                    "capture": "/evidence/paper-capture",
                    "inbox": "/evidence/paper-inbox",
                },
            },
            "risk_policy": {"sha256": _digest("risk-policy")},
            "routes": {"sha256": _digest("routes")},
            "seed": {"sha256": _digest("seed")},
            "risk_policy_sha256": _digest("risk-policy"),
            "routes_sha256": _digest("routes"),
            "seed_sha256": _digest("seed"),
            "calibration_artifact_sha256": _artifact("execution_twin_calibration"),
            "archive_map_artifact_sha256": _digest("archive-map"),
            "baseline_config_artifact_sha256": _digest("baseline-config"),
            "stress_config_artifact_sha256": _digest("stress-config"),
            "candidate_universe_artifact_sha256": _digest("candidate-universe"),
            "candidate_universe_file_sha256": _digest("candidate-universe-file"),
            "candidate_universe_path": "/evidence/candidate-universe.json",
            "demo_rules_artifact_sha256": _artifact("demo_rule_probe"),
            "demo_rules_file_sha256": _file_digest("demo_rule_probe"),
            "rule_coverage_artifact_sha256": _artifact("candidate_rule_coverage"),
            "clock_artifact_sha256": _digest("clock-artifact"),
            "clock_file_sha256": _digest("clock-file"),
            "reset_archive_sha256": _digest("reset-archive"),
            "reset_receipt_artifact_sha256": _digest("reset-receipt-artifact"),
            "reset_receipt_file_sha256": _digest("reset-receipt-file"),
            "reset_started_ts_ns": T0_NS - 20,
            "reset_finished_ts_ns": T0_NS - 10,
            "fresh_roots_verified_at_reset": True,
            "reset_account_epoch_roots": [
                DEMO_ACCOUNT_ROOT,
                "/evidence/demo-inbox",
                DEMO_CAPTURE_ROOT,
                "/evidence/paper-account",
                "/evidence/paper-inbox",
                "/evidence/paper-capture",
            ],
            "paper_owner_first_artifact_sha256": _artifact("paper_owner_start_sequence"),
            "demo_owner_first_artifact_sha256": _artifact("demo_owner_start_sequence"),
            "registered_preseal_source_files": [
                {
                    "path": "/evidence/natural-freeze.json",
                    "sha256": _file_digest("natural_cutover_freeze_manifest"),
                }
            ],
            "registered_preseal_source_roots": [
                "/archive/v7-account",
                "/archive/v7-capture",
            ],
        }
    if role == "stopped_natural_epoch":
        old_paths = _old_path_identities()
        return {
            **common,
            "created_ts_ns": NOW_NS + 10,
            "seal_id": "stopped-natural-epoch-v1",
            "manifest_path": STOPPED_SEAL_PATH,
            "seal_artifact_sha256": _artifact("stopped_natural_epoch"),
            "candidate_commit": COMMIT,
            "origin_main_commit": ORIGIN_MAIN,
            "freeze_id": FREEZE_ID,
            "freeze_manifest": {
                "path": "/evidence/natural-freeze.json",
                "sha256": _file_digest("natural_cutover_freeze_manifest"),
                "artifact_sha256": _artifact("natural_cutover_freeze_manifest"),
            },
            "old_sealed_paths": old_paths,
            "old_sealed_paths_sha256": authority._path_identity_set_sha256(old_paths),
            "old_sealed_root_paths": list(OLD_ROOT_PATHS),
            "old_sealed_root_paths_sha256": authority._sequence_sha256(OLD_ROOT_PATHS),
            "old_mutable_root_roles": [
                "demo_account",
                "demo_inbox",
                "demo_capture",
                "paper_account",
                "paper_inbox",
                "paper_capture",
                "long_demo",
                "long_paper",
                "continuous_demo",
                "continuous_paper",
                "natural_evidence",
            ],
            "old_mutable_files": ["/evidence/natural-evidence/target-capture.jsonl"],
            "t0_ns": T0_NS,
            "t1_ns": T1_NS,
            "interval": "half_open_[t0,t1)",
            "inputs": {"natural_cutover_freeze_manifest": {"path": "/evidence/natural-freeze.json"}},
            "source_files": {
                "freeze_manifest": {
                    "path": "/evidence/natural-freeze.json",
                    "sha256": _file_digest("natural_cutover_freeze_manifest"),
                }
            },
            "source_trees": {
                tree_role: {
                    "root_identity": _path_identity(OLD_ROOT_PATHS[index], index),
                    "entries": (
                        [
                            {
                                "relative_path": "target-capture.jsonl",
                                "kind": "file",
                                "sha256": _digest("target-capture-bytes"),
                            }
                        ]
                        if tree_role == "natural_evidence"
                        else []
                    ),
                }
                for index, tree_role in enumerate(
                    (
                        "demo_account",
                        "demo_inbox",
                        "demo_capture",
                        "paper_account",
                        "paper_inbox",
                        "paper_capture",
                        "long_demo",
                        "long_paper",
                        "continuous_demo",
                        "continuous_paper",
                        "natural_evidence",
                    )
                )
            },
            "tape_semantics": {
                "natural_window": "half_open_[t0,t1)",
                "safety_batches_excluded": True,
            },
            "service_state": {
                "liquidity-migration-account-execution.service": "inactive",
                "liquidity-migration-account-paper-execution.service": "inactive",
            },
            "all_units_stopped": True,
            "execution_authorization": "not_granted",
        }
    if role == "fresh_deploy_epoch":
        old_paths = _old_path_identities()
        return {
            **common,
            "created_ts_ns": NOW_NS + 100,
            "epoch_id": "fresh-deploy-aaaaaaaaaaaa-2000000000002",
            "candidate_commit": COMMIT,
            "freeze_id": FREEZE_ID,
            "manifest_path": "/evidence/fresh-deploy-epoch.json",
            "epoch_parent": "/fresh",
            "stopped_epoch_seal": _stopped_seal_identity(),
            "old_sealed_root_paths": list(OLD_ROOT_PATHS),
            "old_sealed_root_paths_sha256": authority._sequence_sha256(OLD_ROOT_PATHS),
            "old_sealed_paths": old_paths,
            "old_sealed_paths_sha256": authority._path_identity_set_sha256(old_paths),
            "roots": _fresh_root_identities(),
            "late_environment": {
                "liquidity-migration-account-execution.service": {"ACCOUNT_EXECUTION_ROOT": "/fresh/demo-account"}
            },
            "execution_authorization": "not_granted",
        }
    if role == "candidate_rule_coverage":
        return {
            **common,
            "candidate_universe_artifact_sha256": _digest("candidate-universe"),
            "candidate_universe_file_sha256": _digest("candidate-universe-file"),
            "demo_rules_artifact_sha256": _artifact("demo_rule_probe"),
            "demo_rules_file_sha256": _file_digest("demo_rule_probe"),
            "symbol_count": 2,
            "symbol_set_sha256": authority._sequence_sha256(["BTCUSDT", "ETHUSDT"]),
        }
    if role == "demo_rule_probe":
        return {
            **common,
            "verified_ts_ns": NOW_NS - 1,
            "candidate_universe_artifact_sha256": _digest("candidate-universe"),
            "symbol_count": 2,
            "symbol_set_sha256": authority._sequence_sha256(["BTCUSDT", "ETHUSDT"]),
        }
    if role == "execution_twin_calibration":
        return {
            **common,
            "schema_version": 2,
            "account_id": DEMO_ACCOUNT_ID,
            "account_journal_sha256": _digest("v7-journal"),
            "account_last_event_hash": _digest("v7-head"),
            "market_capture_manifest_sha256": _digest("v7-capture"),
            "market_order_smoke_gate_passed": True,
            "partial_fill_calibration_gate_passed": True,
            "sample_counts": {"targets": 30},
        }
    if role == "natural_tape_sufficiency":
        return {
            **common,
            **post_seal,
            "analysis_source_files": [
                *post_seal["analysis_source_files"],
                {
                    "path": f"{evidence_root}/captured_account_replay.json",
                    "sha256": _file_digest("captured_account_replay"),
                },
            ],
            "account_id": DEMO_ACCOUNT_ID,
            "t0_ns": T0_NS,
            "t1_ns": T1_NS,
            "freeze_id": FREEZE_ID,
            "freeze_artifact_sha256": _artifact("natural_cutover_freeze_manifest"),
            "candidate_commit": COMMIT,
            "origin_main_commit": ORIGIN_MAIN,
            "clock_artifact_sha256": _digest("clock-artifact"),
            "clock_file_sha256": _digest("clock-file"),
            "demo_account_root": DEMO_ACCOUNT_ROOT,
            "source_set_sha256": _digest("natural-sources"),
            "target_capture_tape_hash": _digest("target-capture"),
            "target_capture_file_sha256": _digest("target-capture-bytes"),
            "natural_batch_ids_sha256": _batch_hash(),
            "natural_batch_count": 2,
            "safety_batch_ids_sha256": _safety_hash(),
            "account_replay_artifact_sha256": _artifact("captured_account_replay"),
            "venue_accounting_artifact_sha256": _artifact("venue_accounting_reconciliation"),
            **_effective_config_bindings(),
        }
    if role == "captured_account_replay":
        return {
            **common,
            **post_seal,
            "t0_ns": T0_NS,
            "t1_ns": T1_NS,
            "freeze_id": FREEZE_ID,
            "freeze_artifact_sha256": _artifact("natural_cutover_freeze_manifest"),
            "freeze_manifest_file_sha256": _file_digest("natural_cutover_freeze_manifest"),
            "candidate_commit": COMMIT,
            "origin_main_commit": ORIGIN_MAIN,
            "clock_artifact_sha256": _digest("clock-artifact"),
            "clock_file_sha256": _digest("clock-file"),
            "routes_sha256": _digest("routes"),
            "risk_policy_sha256": _digest("risk-policy"),
            "seed_sha256": _digest("seed"),
            "demo_rules_artifact_sha256": _artifact("demo_rule_probe"),
            "demo_rules_file_sha256": _file_digest("demo_rule_probe"),
            "risk_policy_file_sha256": _digest("runtime-risk-policy-file"),
            "demo_account_root": DEMO_ACCOUNT_ROOT,
            "market_capture_root": DEMO_CAPTURE_ROOT,
            "target_capture_tape_hash": _digest("target-capture"),
            "target_capture_file_sha256": _digest("target-capture-bytes"),
            "natural_batch_ids_sha256": _batch_hash(),
            "natural_batch_count": 2,
            "calibration_artifact_sha256": _artifact("execution_twin_calibration"),
            "historical_root": HISTORICAL_ROOT,
            "paper_root": PAPER_ROOT,
            "historical_journal_sha256": _digest("historical-journal"),
            "paper_journal_sha256": _digest("paper-journal"),
            "replay_outputs": _captured_replay_outputs(),
            **_effective_config_bindings(),
        }
    if role == "execution_twin_drift":
        return {
            **common,
            **post_seal,
            "account_id": DEMO_ACCOUNT_ID,
            "freeze_id": FREEZE_ID,
            "freeze_artifact_sha256": _artifact("natural_cutover_freeze_manifest"),
            "freeze_manifest_file_sha256": _file_digest("natural_cutover_freeze_manifest"),
            "t0_ns": T0_NS,
            "t1_ns": T1_NS,
            "natural_batch_ids_sha256": _batch_hash(),
            "safety_batch_ids_sha256": _safety_hash(),
            "safety_batches_excluded": True,
            "calibration_artifact_sha256": _artifact("execution_twin_calibration"),
            "archive_map_artifact_sha256": _digest("archive-map"),
            "baseline_config_artifact_sha256": _digest("baseline-config"),
            "stress_config_artifact_sha256": _digest("stress-config"),
            "natural_account_root": DEMO_ACCOUNT_ROOT,
            "natural_market_capture_root": DEMO_CAPTURE_ROOT,
            "natural_journal_sha256": _digest("drift-stream-journal"),
            "natural_last_event_hash": _digest("natural-head"),
            "natural_target_capture_tape_hash": _digest("target-capture"),
            "natural_target_capture_file_sha256": _digest("target-capture-bytes"),
            "initial_clock_receipt_artifact_sha256": _digest("clock-artifact"),
            "clock_offset_series_artifact_sha256": _digest("clock-series-artifact"),
            "clock_offset_series_file_sha256": _digest("clock-series-file"),
            "clock_offset_series_sample_count": 22,
            "clock_offset_series_max_observed_gap_ns": 6 * 60 * 60 * 1_000_000_000,
            "clock_offset_series_t0_bracketed": True,
            "clock_offset_series_t1_bracketed": True,
            "clock_uncertainty_is_hard_bound": False,
        }
    if role == "event_clock_comparison":
        counts = {"historical": 2, "paper": 2, "demo": 2}
        replay_provenance = _event_replay_provenance()
        return {
            **common,
            **post_seal,
            "event_counts": counts,
            "decision_outcome_counts": counts,
            "replay_input_sha256": _digest("target-capture-bytes"),
            "event_replay_provenance": replay_provenance,
            "event_replay_manifest": replay_provenance["replay_manifest"],
            "event_canonical_source_capture": replay_provenance["canonical_source_capture"],
            "target_replay_output_root": "/derived-target-replay",
            "analysis_source_files": [
                {
                    "path": replay_provenance["replay_manifest"]["path"],
                    "sha256": replay_provenance["replay_manifest"]["sha256"],
                },
                {
                    "path": replay_provenance["canonical_source_capture"]["path"],
                    "sha256": replay_provenance["canonical_source_capture"]["sha256"],
                },
            ],
            "analysis_derived_output_files": _target_replay_output_files(),
        }
    if role == "kernel_parity":
        event_provenance = _event_replay_provenance()
        replay_receipt = {
            "path": f"{evidence_root}/captured_account_replay.json",
            "sha256": _file_digest("captured_account_replay"),
        }
        event_receipt = {
            "path": f"{evidence_root}/event_clock_comparison.json",
            "sha256": _file_digest("event_clock_comparison"),
        }
        return {
            **common,
            **post_seal,
            "authorized_commit": COMMIT,
            "contract_id": "account-kernel-strategy-order-plan-parity-v4",
            "schema_version": 4,
            "quantity_abs_tolerance": 1e-12,
            "natural_batch_ids_sha256": _batch_hash(),
            "natural_batch_count": 2,
            "event_parity_receipt_file_sha256": _file_digest("event_clock_comparison"),
            "calibration_receipt_file_sha256": _file_digest("execution_twin_calibration"),
            "fresh_epoch_reset_receipt_file_sha256": _digest("reset-receipt-file"),
            "risk_policy_file_sha256": _digest("runtime-risk-policy-file"),
            "rules_file_sha256": _file_digest("demo_rule_probe"),
            "comparison_scope_file_sha256": _digest("comparison-scope-file"),
            "comparison_scope_file_path": "/derived-evidence/kernel-comparison-scope.json",
            "comparison_scope_artifact_sha256": _digest("comparison-scope-artifact"),
            "scope_captured_account_replay_receipt": replay_receipt,
            "scope_event_parity_receipt": event_receipt,
            "scope_captured_replay_outputs": _captured_replay_outputs(),
            "scope_event_replay_provenance": event_provenance,
            "demo_journal_stream_sha256": _digest("drift-stream-journal"),
            "historical_root": HISTORICAL_ROOT,
            "paper_root": PAPER_ROOT,
            "demo_root": DEMO_ACCOUNT_ROOT,
            "historical_journal_sha256": _digest("historical-journal"),
            "paper_journal_sha256": _digest("paper-journal"),
            "demo_journal_sha256": _digest("demo-journal"),
            "analysis_source_files": [
                {
                    "path": "/evidence/natural-evidence/target-capture.jsonl",
                    "sha256": _digest("target-capture-bytes"),
                },
                replay_receipt,
                event_receipt,
                {
                    "path": "/derived-evidence/kernel-comparison-scope.json",
                    "sha256": _digest("comparison-scope-file"),
                },
            ],
            "analysis_source_roots": [
                HISTORICAL_ROOT,
                PAPER_ROOT,
                DEMO_ACCOUNT_ROOT,
            ],
            **_effective_config_bindings(),
        }
    if role in authority._VENUE_RECEIPT_ROLES:
        return {
            **common,
            "account_id": DEMO_ACCOUNT_ID,
            "account_root": DEMO_ACCOUNT_ROOT,
            "journal_sha256": _digest("demo-journal"),
            "query_start_ms": T0_NS // 1_000_000,
            "query_end_ms": T1_NS // 1_000_000,
            "venue_accounting_gate_passed": True,
            "final_demo_flatness_gate_passed": True,
            "gate_field": (
                "venue_accounting_gate_passed"
                if role == "venue_accounting_reconciliation"
                else "final_demo_flatness_gate_passed"
            ),
            "sample_counts": {"canonical_fills": 30},
        }
    return {
        "role": role,
        "reviewed_by": "cutover-owner",
        "reviewed_ts_ns": NOW_NS,
    }


def _fake_machine_validation(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
    assert path.is_file()
    assert now_ns == NOW_NS
    bindings = _fake_bindings(role, evidence_root=str(path.parent))
    if role in {"captured_account_replay", "event_clock_comparison"}:
        bindings["receipt_file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if role == "natural_tape_sufficiency":
        dependency_path = path.parent / "captured_account_replay.json"
        dependency_hash = hashlib.sha256(dependency_path.read_bytes()).hexdigest()
        for source in bindings["analysis_source_files"]:
            if source["path"] == str(dependency_path):
                source["sha256"] = dependency_hash
    if role == "kernel_parity":
        for dependency_role, field in (
            (
                "captured_account_replay",
                "scope_captured_account_replay_receipt",
            ),
            ("event_clock_comparison", "scope_event_parity_receipt"),
        ):
            dependency_path = path.parent / f"{dependency_role}.json"
            dependency_hash = hashlib.sha256(dependency_path.read_bytes()).hexdigest()
            bindings[field] = {
                "path": str(dependency_path),
                "sha256": dependency_hash,
            }
            for source in bindings["analysis_source_files"]:
                if source["path"] == str(dependency_path):
                    source["sha256"] = dependency_hash
        bindings["event_parity_receipt_file_sha256"] = bindings["scope_event_parity_receipt"]["sha256"]
    return {
        "validator": (
            authority.MACHINE_VALIDATOR_IDS[role]
            if role in authority.MACHINE_VALIDATED_ROLES
            else "operator_reviewed_evidence_v1"
        ),
        "status": ("passed" if role in authority.MACHINE_VALIDATED_ROLES else "operator_reviewed_integrity_only"),
        "artifact_sha256": _artifact(role),
        "bindings": bindings,
    }


def _central_gate_fixture() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    checks = {
        role: {
            "validator": authority.MACHINE_VALIDATOR_IDS.get(role, "operator_reviewed_evidence_v1"),
            "status": "passed",
            "artifact_sha256": _artifact(role),
            "bindings": _fake_bindings(role),
        }
        for role in authority.ALL_EVIDENCE_ROLES
    }
    evidence = {
        role: {
            "role": role,
            "path": f"/derived-evidence/{role}.json",
            "sha256": _file_digest(role),
        }
        for role in authority.ALL_EVIDENCE_ROLES
    }
    return checks, evidence


def _authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Path, Path]:
    assessment, assessment_path = _passing_assessment(tmp_path)
    machine_id = _machine_id(tmp_path)
    monkeypatch.setattr(authority, "_machine_validate_evidence", _fake_machine_validation)
    monkeypatch.setattr(
        authority,
        "require_clean_authorized_checkout",
        lambda _root, commit: commit,
    )
    monkeypatch.setattr(
        authority,
        "require_fast_forward_candidate",
        lambda _root, **_kwargs: None,
    )
    monkeypatch.setattr(
        authority,
        "require_remote_origin_main",
        lambda _root, commit, **_kwargs: commit,
    )
    receipt = authority.build_authorization_receipt(
        assessment,
        assessment_path=assessment_path,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        issued_ts_ns=NOW_NS,
        lifetime_seconds=3600,
    )
    receipt_path = tmp_path / "account-execution-deploy-ready"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(receipt_path, 0o600)
    return receipt, receipt_path, machine_id


def test_reviewed_evidence_binds_sources_without_claiming_machine_proof(
    tmp_path: Path,
) -> None:
    source = tmp_path / "inventory.json"
    source.write_text('{"units": []}\n', encoding="utf-8")
    receipt = authority.build_reviewed_evidence(
        role="topology_inventory",
        claim="No retired mutator remained installed or active.",
        reviewed_by="operator",
        source_paths=[source],
        reviewed_ts_ns=NOW_NS,
    )

    assert receipt["review_type"] == "operator_attestation_with_source_hashes"
    assert authority.verify_reviewed_evidence(receipt, expected_role="topology_inventory") == receipt

    source.write_text('{"units": ["late-mutator"]}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="changed after review"):
        authority.verify_reviewed_evidence(receipt, expected_role="topology_inventory")


def test_machine_roles_cannot_use_operator_review_wrappers(tmp_path: Path) -> None:
    source = tmp_path / "events.jsonl"
    source.write_text("{}\n", encoding="utf-8")

    for role in (
        "event_clock_comparison",
        "natural_tape_sufficiency",
        "execution_twin_drift",
        "candidate_rule_coverage",
        "stopped_natural_epoch",
        "fresh_deploy_epoch",
    ):
        with pytest.raises(ValueError, match="not an operator-reviewed"):
            authority.build_reviewed_evidence(
                role=role,
                claim="operator says it passed",
                reviewed_by="operator",
                source_paths=[source],
                reviewed_ts_ns=NOW_NS,
            )


def test_event_authority_rejects_exploratory_unbound_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = tmp_path / "exploratory-event-parity.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        strategy_event_parity,
        "load_strategy_event_parity_receipt",
        lambda _path: {
            "strategy_event_replay_gate_passed": True,
            "replay_provenance": {
                "deployment_valid": False,
                "replay_manifest": None,
                "canonical_source_capture": None,
            },
        },
    )

    with pytest.raises(ValueError, match="lacks deployment-valid target-replay provenance"):
        authority._machine_validate_evidence(
            role="event_clock_comparison",
            path=receipt_path,
            now_ns=NOW_NS,
        )


def test_event_authority_parses_the_exact_bound_replay_manifest_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = tmp_path / "event-parity.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    manifest_path = tmp_path / "replay_manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    canonical_capture_hash = _digest("canonical-capture")
    manifest = {
        "path": str(manifest_path),
        "size_bytes": manifest_path.stat().st_size,
        # A valid digest with bytes that deliberately do not match the file.
        "sha256": _digest("different-manifest-bytes"),
        "schema_version": 2,
        "artifact_sha256": _digest("manifest-artifact"),
        "created_ts_ns": NOW_NS,
    }
    canonical_capture = {
        "path": str(tmp_path / "capture.jsonl"),
        "size_bytes": 100,
        "sha256": canonical_capture_hash,
        "device": 1,
        "inode": 2,
        "mtime_ns": NOW_NS,
        "mode": 0o600,
        "uid": os.geteuid(),
        "nlink": 1,
        "capture_event_count": 1,
        "capture_chain_hash": _digest("capture-chain"),
        "source_environment": "demo",
    }
    sources = {
        environment: {
            "event_tape": {"event_count": 1},
            "decision_tape": {"outcome_count": 1},
            "replay_input": {"sha256": canonical_capture_hash},
        }
        for environment in ("historical", "paper", "demo")
    }

    def load_event(_path: Path, *, snapshot: Any = None) -> dict[str, Any]:
        assert snapshot is not None
        return {
            "strategy_event_replay_gate_passed": True,
            "replay_provenance": {
                "deployment_valid": True,
                "replay_manifest": manifest,
                "canonical_source_capture": canonical_capture,
            },
            "sources": sources,
        }

    monkeypatch.setattr(
        strategy_event_parity,
        "load_strategy_event_parity_receipt",
        load_event,
    )

    with pytest.raises(ValueError, match="bytes differ from the bound provenance"):
        authority._machine_validate_evidence(
            role="event_clock_comparison",
            path=receipt_path,
            now_ns=NOW_NS,
        )


def test_mutable_owner_health_roles_are_removed(tmp_path: Path) -> None:
    source = tmp_path / "account_owner_health.json"
    source.write_text("{}\n", encoding="utf-8")
    assert "demo_owner_health_snapshot" not in authority.ALL_EVIDENCE_ROLES
    assert "paper_owner_health_snapshot" not in authority.ALL_EVIDENCE_ROLES
    with pytest.raises(ValueError, match="not an operator-reviewed"):
        authority.build_reviewed_evidence(
            role="demo_owner_health_snapshot",
            claim="current health is good",
            reviewed_by="operator",
            source_paths=[source],
            reviewed_ts_ns=NOW_NS,
        )


def test_authorization_binds_host_full_commit_expiry_and_exact_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, receipt_path, machine_id = _authorization(tmp_path, monkeypatch)

    verified = authority.load_authorization_receipt(
        receipt_path,
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        now_ns=NOW_NS + 1,
    )
    assert verified == receipt
    assert verified["schema_version"] == 3
    assert set(verified["gates"]) == set(authority.REQUIRED_GATE_ROLES)
    assert verified["aggregate_check"]["status"] == "passed"
    aggregate_checks = verified["aggregate_check"]["checks"]
    assert aggregate_checks["declared_analysis_chronology_consistent"] is True
    assert aggregate_checks["declared_fresh_epoch_chronology_consistent"] is True
    assert aggregate_checks["analysis_sources_reopened_with_exact_hashes"] is True
    assert aggregate_checks["analysis_dependency_receipts_exactly_linked"] is True
    assert "post_seal_analysis_timestamps_after_stopped_seal" not in aggregate_checks
    assert "fresh_epoch_created_after_stopped_seal" not in aggregate_checks

    with pytest.raises(ValueError, match="40-character"):
        authority.verify_authorization_receipt(
            receipt,
            expected_commit=COMMIT[:12],
            repo_root=tmp_path,
            machine_id_path=machine_id,
            now_ns=NOW_NS + 1,
        )
    with pytest.raises(ValueError, match="another host"):
        authority.verify_authorization_receipt(
            receipt,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path, "machine-b"),
            now_ns=NOW_NS + 1,
        )
    with pytest.raises(ValueError, match="expired"):
        authority.verify_authorization_receipt(
            receipt,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            now_ns=receipt["expires_ts_ns"] + 1,
        )


def test_v7_training_role_cannot_satisfy_natural_holdout_gate(tmp_path: Path) -> None:
    assessment, _path = _passing_assessment(tmp_path)
    gate = assessment["gates"]["natural_120h_target_order_fill_pnl_tape"]
    gate["evidence"] = [
        "execution_twin_calibration",
        "captured_account_replay",
        "venue_accounting_reconciliation",
    ]
    with pytest.raises(ValueError, match="evidence roles differ"):
        authority._validate_assessment_structure(assessment)


def test_v7_artifact_cannot_alias_natural_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)
    machine_id = _machine_id(tmp_path)

    def aliased(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == "natural_tape_sufficiency":
            check["artifact_sha256"] = _artifact("execution_twin_calibration")
        return check

    monkeypatch.setattr(authority, "_machine_validate_evidence", aliased)
    with pytest.raises(ValueError, match="V7 calibration artifact aliases"):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            issued_ts_ns=NOW_NS,
        )


def test_inconclusive_natural_receipt_is_not_machine_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = tmp_path / "natural.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        natural_sufficiency,
        "load_natural_tape_sufficiency_receipt",
        lambda _path: {
            "integrity_gate_passed": True,
            "sufficiency_gate_passed": False,
            "status": "inconclusive",
        },
    )
    with pytest.raises(ValueError, match="not a sufficient pass"):
        authority._machine_validate_evidence(
            role="natural_tape_sufficiency",
            path=receipt_path,
            now_ns=NOW_NS,
        )


def test_fresh_epoch_machine_validation_is_poststart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = tmp_path / "fresh-deploy-epoch.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    old_paths = _old_path_identities()
    observed_empty_requirements: list[bool] = []

    def load(_path: Path, *, require_empty_roots: bool) -> dict[str, Any]:
        observed_empty_requirements.append(require_empty_roots)
        return {
            "artifact_sha256": _artifact("fresh_deploy_epoch"),
            "created_ts_ns": NOW_NS + 2,
            "epoch_id": "fresh-deploy-aaaaaaaaaaaa-2000000000002",
            "candidate_commit": COMMIT,
            "freeze_id": FREEZE_ID,
            "epoch_parent": "/fresh",
            "stopped_epoch_seal": _stopped_seal_identity(),
            "old_sealed_paths": old_paths,
            "old_sealed_paths_sha256": authority._path_identity_set_sha256(old_paths),
            "roots": _fresh_root_identities(),
            "late_environment": {
                "liquidity-migration-account-execution.service": {"ACCOUNT_EXECUTION_ROOT": "/fresh/demo-account"}
            },
            "execution_authorization": "not_granted",
        }

    monkeypatch.setattr(fresh_deploy_epoch, "load_fresh_deploy_epoch", load)
    check = authority._machine_validate_evidence(
        role="fresh_deploy_epoch",
        path=receipt_path,
        now_ns=NOW_NS,
    )
    assert observed_empty_requirements == [False]
    assert check["bindings"]["roots"] == _fresh_root_identities()


def test_stopped_epoch_machine_validation_is_poststart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = tmp_path / "stopped-natural-epoch.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    roles = [
        "demo_account",
        "demo_inbox",
        "demo_capture",
        "paper_account",
        "paper_inbox",
        "paper_capture",
        "long_demo",
        "long_paper",
        "continuous_demo",
        "continuous_paper",
        "natural_evidence",
    ]
    observed_stopped_requirements: list[bool] = []

    def load(
        _path: Path,
        *,
        require_currently_stopped: bool = False,
        systemctl_bin: str = "systemctl",
    ) -> dict[str, Any]:
        assert systemctl_bin == "systemctl"
        observed_stopped_requirements.append(require_currently_stopped)
        source_trees = {
            role: {
                "role": role,
                "path": OLD_ROOT_PATHS[index],
                "root_identity": {
                    "path": OLD_ROOT_PATHS[index],
                    "device": 7,
                    "inode": 10_000 + index,
                    "mtime_ns": NOW_NS,
                    "mode": 0o700,
                    "uid": 501,
                },
                "entries": [],
                "tree_sha256": _digest(f"tree:{role}"),
            }
            for index, role in enumerate(roles)
        }
        return {
            "schema_version": 1,
            "kind": "stopped_natural_epoch_seal",
            "validator": "stopped_natural_epoch_v1",
            "created_ts_ns": NOW_NS + 1,
            "identity": {
                "repository_root": "/repo",
                "candidate_commit": COMMIT,
                "origin_main_commit": ORIGIN_MAIN,
                "freeze_id": FREEZE_ID,
                "freeze_artifact_sha256": _artifact("natural_cutover_freeze_manifest"),
                "t0_ns": T0_NS,
                "t1_ns": T1_NS,
                "interval": "half_open_[t0,t1)",
            },
            "inputs": {"freeze_manifest": {"path": "/evidence/natural-freeze.json"}},
            "sealed_namespace": {
                "required_old_mutable_roots": [
                    {"role": role, "path": OLD_ROOT_PATHS[index]} for index, role in enumerate(roles)
                ],
                "required_old_mutable_files": ["/evidence/natural-evidence/target-capture.jsonl"],
                "root_count": 11,
                "file_count": 1,
            },
            "source_files": {
                "freeze_manifest": {
                    "path": "/evidence/natural-freeze.json",
                    "sha256": _file_digest("natural_cutover_freeze_manifest"),
                }
            },
            "source_trees": source_trees,
            "tape_semantics": {"natural_target_capture": {"interval": "half_open_[t0,t1)"}},
            "service_state": {
                "required_units": ["liquidity-migration-account-execution.service"],
                "before_hashing": {},
                "after_hashing": {},
                "all_inactive_before_hashing": True,
                "all_inactive_after_hashing": True,
            },
            "execution_authorization": "not_granted",
            "artifact_sha256": _artifact("stopped_natural_epoch"),
        }

    module = ModuleType("liquidity_migration.stopped_natural_epoch")
    module.load_stopped_natural_epoch_seal = load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    check = authority._machine_validate_evidence(
        role="stopped_natural_epoch",
        path=receipt_path,
        now_ns=NOW_NS,
    )
    assert observed_stopped_requirements == [False]
    assert check["bindings"]["old_sealed_root_paths"] == OLD_ROOT_PATHS
    assert check["bindings"]["all_units_stopped"] is True


def test_aggregate_rejects_mixed_natural_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)

    def mixed(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == "execution_twin_drift":
            check["bindings"]["t1_ns"] = T1_NS + 1
        return check

    monkeypatch.setattr(authority, "_machine_validate_evidence", mixed)
    with pytest.raises(ValueError, match="natural_window_t1.*differs"):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path),
            issued_ts_ns=NOW_NS,
        )


def test_aggregate_rejects_event_parity_from_another_target_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)

    def swapped(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == "event_clock_comparison":
            check["bindings"]["replay_input_sha256"] = _digest("another-target-capture")
        return check

    monkeypatch.setattr(authority, "_machine_validate_evidence", swapped)
    with pytest.raises(ValueError, match="event replay input.*differs"):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path),
            issued_ts_ns=NOW_NS,
        )


def test_sealed_source_gate_separates_declared_chronology_from_dependencies() -> None:
    checks, evidence = _central_gate_fixture()

    result = authority._sealed_source_analysis_gate(
        evidence=evidence,
        checks=checks,
        stopped=checks["stopped_natural_epoch"]["bindings"],
        fresh=checks["fresh_deploy_epoch"]["bindings"],
        freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
    )

    assert result == {
        "analysis_receipts_outside_epoch_roots": True,
        "declared_analysis_chronology_consistent": True,
        "analysis_sources_reopened_with_exact_hashes": True,
        "analysis_dependency_receipts_exactly_linked": True,
    }


@pytest.mark.parametrize(
    ("role", "completed", "error"),
    [
        ("captured_account_replay", 0, "lacks a positive declared completion timestamp"),
        ("event_clock_comparison", NOW_NS + 10, "inconsistent declared chronology"),
    ],
)
def test_sealed_source_gate_rejects_invalid_declared_chronology(
    role: str,
    completed: int,
    error: str,
) -> None:
    checks, evidence = _central_gate_fixture()
    checks[role]["bindings"]["declared_analysis_completed_ts_ns"] = completed

    with pytest.raises(ValueError, match=error):
        authority._sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=checks["stopped_natural_epoch"]["bindings"],
            fresh=checks["fresh_deploy_epoch"]["bindings"],
            freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
        )


@pytest.mark.parametrize("epoch_root", [DEMO_ACCOUNT_ROOT, "/fresh/demo-account"])
def test_sealed_source_gate_rejects_receipt_inside_epoch_root(
    epoch_root: str,
) -> None:
    checks, evidence = _central_gate_fixture()
    evidence["captured_account_replay"]["path"] = f"{epoch_root}/receipt.json"

    with pytest.raises(ValueError, match="inside an epoch root"):
        authority._sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=checks["stopped_natural_epoch"]["bindings"],
            fresh=checks["fresh_deploy_epoch"]["bindings"],
            freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
        )


@pytest.mark.parametrize(
    ("source", "error"),
    [
        (
            {
                "path": "/derived-target-replay/historical/events.jsonl",
                "sha256": _digest("target-replay-event-tape"),
            },
            "not registered or dependency-bound",
        ),
        (
            {
                "path": "/evidence/natural-evidence/target-capture.jsonl",
                "sha256": _digest("mutated-target"),
            },
            "differs from its registered identity",
        ),
    ],
)
def test_sealed_source_gate_rejects_unregistered_or_mutated_source(
    source: dict[str, str],
    error: str,
) -> None:
    checks, evidence = _central_gate_fixture()
    checks["event_clock_comparison"]["bindings"]["analysis_source_files"] = [source]

    with pytest.raises(ValueError, match=error):
        authority._sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=checks["stopped_natural_epoch"]["bindings"],
            fresh=checks["fresh_deploy_epoch"]["bindings"],
            freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
        )


@pytest.mark.parametrize(
    ("role", "dependency_role"),
    [
        ("natural_tape_sufficiency", "captured_account_replay"),
        ("kernel_parity", "event_clock_comparison"),
    ],
)
def test_sealed_source_gate_requires_exact_receipt_dependencies(
    role: str,
    dependency_role: str,
) -> None:
    checks, evidence = _central_gate_fixture()
    dependency_path = evidence[dependency_role]["path"]
    sources = checks[role]["bindings"]["analysis_source_files"]
    checks[role]["bindings"]["analysis_source_files"] = [
        source for source in sources if source["path"] != dependency_path
    ]

    with pytest.raises(ValueError, match="receipt dependencies differ"):
        authority._sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=checks["stopped_natural_epoch"]["bindings"],
            fresh=checks["fresh_deploy_epoch"]["bindings"],
            freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
        )


def test_sealed_source_gate_checks_manifest_declared_chronology() -> None:
    checks, evidence = _central_gate_fixture()
    event = checks["event_clock_comparison"]["bindings"]
    event["event_replay_manifest"]["created_ts_ns"] = NOW_NS + 10
    event["event_replay_provenance"]["replay_manifest"]["created_ts_ns"] = NOW_NS + 10

    with pytest.raises(ValueError, match="manifest has inconsistent declared chronology"):
        authority._sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=checks["stopped_natural_epoch"]["bindings"],
            fresh=checks["fresh_deploy_epoch"]["bindings"],
            freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
        )


@pytest.mark.parametrize(
    "protected_root",
    [
        DEMO_ACCOUNT_ROOT,
        "/fresh/demo-account",
        "/archive/v7-account",
        HISTORICAL_ROOT,
    ],
)
def test_sealed_source_gate_rejects_target_replay_namespace_overlap(
    protected_root: str,
) -> None:
    checks, evidence = _central_gate_fixture()
    event = checks["event_clock_comparison"]["bindings"]
    manifest_path = f"{protected_root}/target-replay/replay_manifest.json"
    target_root = f"{protected_root}/target-replay"
    event["target_replay_output_root"] = target_root
    event["event_replay_manifest"]["path"] = manifest_path
    event["event_replay_provenance"]["replay_manifest"]["path"] = manifest_path

    with pytest.raises(ValueError, match="target-replay output root overlaps"):
        authority._sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=checks["stopped_natural_epoch"]["bindings"],
            fresh=checks["fresh_deploy_epoch"]["bindings"],
            freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
        )


def test_sealed_source_gate_rejects_derived_output_outside_manifest_root() -> None:
    checks, evidence = _central_gate_fixture()
    checks["event_clock_comparison"]["bindings"]["analysis_derived_output_files"][0]["path"] = (
        "/unrelated-replay-output/events.jsonl"
    )

    with pytest.raises(ValueError, match="outside its target-replay root"):
        authority._sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=checks["stopped_natural_epoch"]["bindings"],
            fresh=checks["fresh_deploy_epoch"]["bindings"],
            freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
        )


@pytest.mark.parametrize(
    "protected_root",
    [
        DEMO_ACCOUNT_ROOT,
        "/fresh/demo-account",
        "/archive/v7-account",
        HISTORICAL_ROOT,
    ],
)
def test_sealed_source_gate_rejects_kernel_scope_in_protected_root(
    protected_root: str,
) -> None:
    checks, evidence = _central_gate_fixture()
    checks["kernel_parity"]["bindings"]["comparison_scope_file_path"] = f"{protected_root}/kernel-scope.json"

    with pytest.raises(ValueError, match="kernel comparison-scope file is inside"):
        authority._sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=checks["stopped_natural_epoch"]["bindings"],
            fresh=checks["fresh_deploy_epoch"]["bindings"],
            freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
        )


@pytest.mark.parametrize(
    ("field", "mutation", "error"),
    [
        (
            "scope_captured_account_replay_receipt",
            {"sha256": _digest("another-captured-replay-receipt")},
            "captured-account replay receipt hash.*differs",
        ),
        (
            "scope_captured_replay_outputs",
            {"paper_final_state_hash": _digest("another-paper-state")},
            "captured-replay outputs.*differs",
        ),
        (
            "scope_event_replay_provenance",
            {"deployment_valid": False},
            "event-replay provenance.*differs",
        ),
    ],
)
def test_sealed_source_gate_rejects_broken_kernel_scope_hash_join(
    field: str,
    mutation: dict[str, Any],
    error: str,
) -> None:
    checks, evidence = _central_gate_fixture()
    checks["kernel_parity"]["bindings"][field].update(mutation)

    with pytest.raises(ValueError, match=error):
        authority._sealed_source_analysis_gate(
            evidence=evidence,
            checks=checks,
            stopped=checks["stopped_natural_epoch"]["bindings"],
            fresh=checks["fresh_deploy_epoch"]["bindings"],
            freeze=checks["natural_cutover_freeze_manifest"]["bindings"],
        )


@pytest.mark.parametrize(
    ("role", "field", "replacement", "error_binding"),
    [
        ("stopped_natural_epoch", "freeze_id", "natural-cutover-other", "natural_freeze_id"),
        ("fresh_deploy_epoch", "candidate_commit", "b" * 40, "authorized_commit"),
    ],
)
def test_aggregate_rejects_mixed_stopped_or_fresh_epochs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    field: str,
    replacement: str,
    error_binding: str,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)

    def mixed(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == test_role:
            check["bindings"][field] = replacement
        return check

    test_role = role
    monkeypatch.setattr(authority, "_machine_validate_evidence", mixed)
    with pytest.raises(ValueError, match=rf"{error_binding}.*differs"):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path),
            issued_ts_ns=NOW_NS,
        )


def test_aggregate_rejects_mismatched_stopped_and_fresh_path_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)

    def substituted(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == "fresh_deploy_epoch":
            replacement = list(check["bindings"]["old_sealed_root_paths"])
            replacement[-1] = "/evidence/another-natural-evidence"
            check["bindings"]["old_sealed_root_paths"] = replacement
            check["bindings"]["old_sealed_root_paths_sha256"] = authority._sequence_sha256(replacement)
        return check

    monkeypatch.setattr(authority, "_machine_validate_evidence", substituted)
    with pytest.raises(ValueError, match="fresh_epoch_old_sealed_root_paths.*differs"):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path),
            issued_ts_ns=NOW_NS,
        )


def test_aggregate_rejects_inconsistent_declared_fresh_epoch_chronology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)

    def time_reversed(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == "fresh_deploy_epoch":
            check["bindings"]["created_ts_ns"] = NOW_NS
        return check

    monkeypatch.setattr(authority, "_machine_validate_evidence", time_reversed)
    with pytest.raises(ValueError, match="inconsistent declared chronology"):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path),
            issued_ts_ns=NOW_NS,
        )


@pytest.mark.parametrize(
    ("role", "mutate", "error"),
    [
        (
            "fresh_deploy_epoch",
            lambda bindings: bindings["stopped_epoch_seal"].__setitem__("sha256", _digest("another-stopped-seal")),
            "fresh_epoch_stopped_seal_file.*differs",
        ),
        (
            "stopped_natural_epoch",
            lambda bindings: bindings.__setitem__("all_units_stopped", False),
            "all-units-stopped",
        ),
        (
            "fresh_deploy_epoch",
            lambda bindings: bindings.__setitem__("execution_authorization", "granted"),
            "grants execution authority",
        ),
    ],
)
def test_aggregate_rejects_epoch_binding_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    mutate: Any,
    error: str,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)

    def tampered(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == test_role:
            mutate(check["bindings"])
        return check

    test_role = role
    monkeypatch.setattr(authority, "_machine_validate_evidence", tampered)
    with pytest.raises(ValueError, match=error):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path),
            issued_ts_ns=NOW_NS,
        )


@pytest.mark.parametrize(
    ("role", "field", "error_binding"),
    [
        (
            "captured_account_replay",
            "freeze_artifact_sha256",
            "natural_freeze_artifact",
        ),
        (
            "execution_twin_drift",
            "initial_clock_receipt_artifact_sha256",
            "natural_clock_series_initial_receipt",
        ),
        (
            "execution_twin_drift",
            "natural_journal_sha256",
            "demo_journal_stream",
        ),
        ("captured_account_replay", "demo_rules_file_sha256", "demo_rules_file"),
        (
            "natural_cutover_freeze_manifest",
            "reset_receipt_file_sha256",
            "fresh_epoch_reset_receipt_file",
        ),
        (
            "captured_account_replay",
            "effective_runtime_config_bundle_file_sha256",
            "effective_config_bundle_file",
        ),
        (
            "natural_tape_sufficiency",
            "effective_runtime_config_run_config_artifact_sha256",
            "effective_config_run_config_artifact",
        ),
        (
            "kernel_parity",
            "effective_runtime_config_candidate_file_sha256",
            "effective_config_candidate_file",
        ),
    ],
)
def test_aggregate_rejects_cross_epoch_runtime_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    field: str,
    error_binding: str,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)

    def swapped(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == test_role:
            check["bindings"][field] = _digest(f"substituted-{field}")
        return check

    test_role = role
    monkeypatch.setattr(authority, "_machine_validate_evidence", swapped)
    with pytest.raises(ValueError, match=rf"{error_binding}.*differs"):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path),
            issued_ts_ns=NOW_NS,
        )


def test_aggregate_requires_reset_time_fresh_root_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)

    def stale(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == "natural_cutover_freeze_manifest":
            check["bindings"]["fresh_roots_verified_at_reset"] = False
        return check

    monkeypatch.setattr(authority, "_machine_validate_evidence", stale)
    with pytest.raises(ValueError, match="did not verify fresh roots"):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path),
            issued_ts_ns=NOW_NS,
        )


def test_aggregate_requires_safety_batches_excluded_from_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)

    def unsafe(*, role: str, path: Path, now_ns: int) -> dict[str, Any]:
        check = _fake_machine_validation(role=role, path=path, now_ns=now_ns)
        if role == "execution_twin_drift":
            check["bindings"]["safety_batches_excluded"] = False
        return check

    monkeypatch.setattr(authority, "_machine_validate_evidence", unsafe)
    with pytest.raises(ValueError, match="did not exclude registered safety"):
        authority.build_authorization_receipt(
            assessment,
            assessment_path=assessment_path,
            repo_root=tmp_path,
            machine_id_path=_machine_id(tmp_path),
            issued_ts_ns=NOW_NS,
        )


def test_duplicate_role_and_incompatible_path_aliases_are_rejected(tmp_path: Path) -> None:
    assessment, _path = _passing_assessment(tmp_path)
    assessment["evidence"]["final_evidence_card"]["role"] = "topology_inventory"
    with pytest.raises(ValueError, match="role 'topology_inventory' is repeated"):
        authority._validate_assessment_structure(assessment)

    assessment, _path = _passing_assessment(tmp_path)
    assessment["evidence"]["final_evidence_card"]["path"] = assessment["evidence"]["topology_inventory"]["path"]
    with pytest.raises(ValueError, match="reused by incompatible roles"):
        authority._validate_assessment_structure(assessment)


def test_authorization_reopens_assessment_and_evidence_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _receipt_path, machine_id = _authorization(tmp_path, monkeypatch)
    evidence_path = Path(next(iter(receipt["evidence"].values()))["path"])
    evidence_path.write_text('{"changed": true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="changed after authorization issuance"):
        authority.verify_authorization_receipt(
            receipt,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            now_ns=NOW_NS + 1,
        )


def test_authorization_reopens_bound_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _receipt_path, machine_id = _authorization(tmp_path, monkeypatch)
    Path(receipt["assessment"]["path"]).write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="assessment changed after authorization issuance"):
        authority.verify_authorization_receipt(
            receipt,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            now_ns=NOW_NS + 1,
        )


def test_rehashed_aggregate_tamper_does_not_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _receipt_path, machine_id = _authorization(tmp_path, monkeypatch)
    changed = json.loads(json.dumps(receipt))
    changed["aggregate_check"]["checks"]["natural_batch_scope"] = False
    changed["artifact_sha256"] = authority._self_hash(changed)
    with pytest.raises(ValueError, match="aggregate check"):
        authority.verify_authorization_receipt(
            changed,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            now_ns=NOW_NS + 1,
        )


@pytest.mark.parametrize("old_version", [1, 2])
def test_legacy_authorization_schemas_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old_version: int,
) -> None:
    receipt, _receipt_path, machine_id = _authorization(tmp_path, monkeypatch)
    changed = json.loads(json.dumps(receipt))
    changed["schema_version"] = old_version
    changed["artifact_sha256"] = authority._self_hash(changed)
    with pytest.raises(ValueError, match="unsupported.*schema"):
        authority.verify_authorization_receipt(
            changed,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            now_ns=NOW_NS + 1,
        )


def test_open_or_wrong_role_gate_cannot_issue(tmp_path: Path) -> None:
    assessment, _path = _passing_assessment(tmp_path)
    assessment["gates"]["venue_pnl_funding_and_final_flatness"]["status"] = "open"
    with pytest.raises(ValueError, match="has not passed"):
        authority._validate_assessment_structure(assessment)

    assessment, _path = _passing_assessment(tmp_path)
    assessment["gates"]["venue_pnl_funding_and_final_flatness"]["evidence"] = ["topology_inventory"]
    with pytest.raises(ValueError, match="evidence roles differ"):
        authority._validate_assessment_structure(assessment)


def test_remote_origin_main_is_checked_at_issue_and_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)
    machine_id = _machine_id(tmp_path)
    observed: list[str] = []
    monkeypatch.setattr(authority, "_machine_validate_evidence", _fake_machine_validation)
    monkeypatch.setattr(
        authority,
        "require_clean_authorized_checkout",
        lambda _root, commit: commit,
    )
    monkeypatch.setattr(
        authority,
        "require_fast_forward_candidate",
        lambda _root, **_kwargs: None,
    )

    def remote(
        _root: str | Path,
        commit: str,
        *,
        promoted_commit: str | None = None,
    ) -> str:
        observed.append(f"{commit}:{promoted_commit or ''}")
        return commit

    monkeypatch.setattr(authority, "require_remote_origin_main", remote)
    receipt = authority.build_authorization_receipt(
        assessment,
        assessment_path=assessment_path,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        issued_ts_ns=NOW_NS,
    )
    authority.verify_authorization_receipt(
        receipt,
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        now_ns=NOW_NS + 1,
    )
    assert observed == [ORIGIN_MAIN + ":", f"{ORIGIN_MAIN}:{COMMIT}"]


def test_issue_and_verify_share_descriptor_snapshots_across_identity_parse_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, assessment_path = _passing_assessment(tmp_path)
    machine_id = _machine_id(tmp_path)
    phase = "issue"
    identity_snapshot_ids: dict[tuple[str, str], set[int]] = {}
    parsed_snapshot_ids: dict[tuple[str, str], set[int]] = {}
    validated_snapshot_ids: dict[tuple[str, str], int] = {}
    original_identity = authority._file_identity
    original_json = authority._load_json_object

    def tracked_identity(
        path: Path,
        *,
        snapshot: Any = None,
        label: str = "evidence file",
    ) -> dict[str, Any]:
        if snapshot is not None:
            identity_snapshot_ids.setdefault((phase, str(path)), set()).add(id(snapshot))
        return original_identity(path, snapshot=snapshot, label=label)

    def tracked_json(
        path: Path,
        *,
        label: str,
        snapshot: Any = None,
    ) -> dict[str, Any]:
        if snapshot is not None:
            parsed_snapshot_ids.setdefault((phase, str(path)), set()).add(id(snapshot))
        return original_json(path, label=label, snapshot=snapshot)

    def validate_snapshot(
        *,
        role: str,
        path: Path,
        now_ns: int,
        snapshot: Any,
    ) -> dict[str, Any]:
        parsed = json.loads(snapshot.data)
        assert isinstance(parsed, dict)
        validated_snapshot_ids[(phase, role)] = id(snapshot)
        parsed_snapshot_ids.setdefault((phase, str(path)), set()).add(id(snapshot))
        return _fake_machine_validation(role=role, path=path, now_ns=now_ns)

    monkeypatch.setattr(authority, "_file_identity", tracked_identity)
    monkeypatch.setattr(authority, "_load_json_object", tracked_json)
    monkeypatch.setattr(authority, "_machine_validate_evidence", validate_snapshot)
    monkeypatch.setattr(
        authority,
        "require_clean_authorized_checkout",
        lambda _root, commit: commit,
    )
    monkeypatch.setattr(
        authority,
        "require_fast_forward_candidate",
        lambda _root, **_kwargs: None,
    )
    monkeypatch.setattr(
        authority,
        "require_remote_origin_main",
        lambda _root, commit, **_kwargs: commit,
    )

    receipt = authority.build_authorization_receipt(
        assessment,
        assessment_path=assessment_path,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        issued_ts_ns=NOW_NS,
    )
    phase = "verify"
    authority.verify_authorization_receipt(
        receipt,
        expected_commit=COMMIT,
        repo_root=tmp_path,
        machine_id_path=machine_id,
        now_ns=NOW_NS + 1,
    )

    for current_phase in ("issue", "verify"):
        assert (
            parsed_snapshot_ids[(current_phase, str(assessment_path))]
            <= identity_snapshot_ids[(current_phase, str(assessment_path))]
        )
        for evidence in receipt["evidence"].values():
            role = str(evidence["role"])
            path = str(evidence["path"])
            snapshot_id = validated_snapshot_ids[(current_phase, role)]
            assert snapshot_id in identity_snapshot_ids[(current_phase, path)]
            assert snapshot_id in parsed_snapshot_ids[(current_phase, path)]


@pytest.mark.parametrize(
    ("observed", "promoted_commit", "accepted"),
    [
        (ORIGIN_MAIN, None, True),
        (COMMIT, None, False),
        (ORIGIN_MAIN, COMMIT, True),
        (COMMIT, COMMIT, True),
        ("c" * 40, COMMIT, False),
    ],
)
def test_remote_main_transition_accepts_only_base_or_exact_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed: str,
    promoted_commit: str | None,
    accepted: bool,
) -> None:
    def run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[-3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="https://github.com/example/private.git\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=f"{observed}\trefs/heads/main\n",
            stderr="",
        )

    monkeypatch.setattr(authority.subprocess, "run", run)
    if accepted:
        assert (
            authority.require_remote_origin_main(
                tmp_path,
                ORIGIN_MAIN,
                promoted_commit=promoted_commit,
            )
            == observed
        )
    else:
        with pytest.raises(ValueError, match="frozen base|authorized promotion"):
            authority.require_remote_origin_main(
                tmp_path,
                ORIGIN_MAIN,
                promoted_commit=promoted_commit,
            )


def test_remote_main_private_github_lookup_uses_ephemeral_token_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network_environments: list[dict[str, str]] = []

    def run(
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if args[-3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="https://github.com/example/private.git\n",
                stderr="",
            )
        network_environments.append(dict(kwargs["env"]))
        assert "secret-token" not in " ".join(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=f"{ORIGIN_MAIN}\trefs/heads/main\n",
            stderr="",
        )

    monkeypatch.setattr(authority.subprocess, "run", run)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    assert authority.require_remote_origin_main(tmp_path, ORIGIN_MAIN) == ORIGIN_MAIN
    assert len(network_environments) == 1
    environment = network_environments[0]
    assert "GITHUB_TOKEN" not in environment
    assert environment["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert environment["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: Basic ")
    assert "secret-token" not in environment["GIT_CONFIG_VALUE_0"]


def test_non_fast_forward_candidate_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    monkeypatch.setattr(authority.subprocess, "run", run)
    with pytest.raises(ValueError, match="not a fast-forward descendant"):
        authority.require_fast_forward_candidate(
            tmp_path,
            frozen_origin_main_commit=ORIGIN_MAIN,
            authorized_commit=COMMIT,
        )


def test_authorization_file_must_be_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _receipt, receipt_path, machine_id = _authorization(tmp_path, monkeypatch)
    os.chmod(receipt_path, 0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        authority.load_authorization_receipt(
            receipt_path,
            expected_commit=COMMIT,
            repo_root=tmp_path,
            machine_id_path=machine_id,
            now_ns=NOW_NS + 1,
        )
